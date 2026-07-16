from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from pathlib import Path

from .store import TaskRecord, TaskStore


def _prepare_output_directory(task: TaskRecord) -> None:
    output_dir = Path(task.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = output_dir / "uploads"
    for child in output_dir.iterdir():
        if child == uploads_dir:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def _run_mineru(task: TaskRecord) -> None:
    # MinerU is imported only inside the isolated worker. The FastAPI control
    # process therefore remains responsive even while models are loaded.
    from mineru.cli.fast_api import ParseRequestOptions, StoredUpload, run_parse_job

    options = task.options
    request_options = ParseRequestOptions(
        files=[],
        lang_list=list(options.get("lang_list") or ["ch"]),
        backend=str(options.get("backend") or task.backend),
        parse_method=str(options.get("parse_method") or "auto"),
        formula_enable=bool(options.get("formula_enable", True)),
        table_enable=bool(options.get("table_enable", True)),
        server_url=options.get("server_url"),
        return_md=bool(options.get("return_md", True)),
        return_middle_json=bool(options.get("return_middle_json", False)),
        return_model_output=bool(options.get("return_model_output", False)),
        return_content_list=bool(options.get("return_content_list", False)),
        return_images=bool(options.get("return_images", False)),
        response_format_zip=bool(options.get("response_format_zip", False)),
        return_original_file=bool(options.get("return_original_file", False)),
        start_page_id=int(options.get("start_page_id", 0)),
        end_page_id=int(options.get("end_page_id", 99999)),
    )
    uploads = [
        StoredUpload(original_name=original_name, stem=stem, path=path)
        for original_name, stem, path in zip(
            task.upload_names,
            task.file_names,
            task.uploads,
        )
    ]
    asyncio.run(
        run_parse_job(
            output_dir=task.output_dir,
            uploads=uploads,
            request_options=request_options,
            config={},
        )
    )

    if request_options.return_md and not any(
        path.is_file() for path in Path(task.output_dir).rglob("*.md")
    ):
        raise RuntimeError("MinerU completed without producing Markdown output")


def worker_main(database_path: str, poll_interval_seconds: float = 0.5) -> None:
    try:
        os.setsid()
    except OSError:
        pass

    stopping = False

    def request_stop(_signum: int, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    store = TaskStore(database_path)
    store.initialize()

    while not stopping:
        task = store.claim_next_pending(os.getpid())
        if task is None:
            time.sleep(max(0.1, poll_interval_seconds))
            continue

        try:
            _prepare_output_directory(task)
            _run_mineru(task)
            store.complete(task.task_id)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            store.fail(task.task_id, str(exc), "PARSE_FAILED")
