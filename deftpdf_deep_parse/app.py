from __future__ import annotations

import asyncio
import multiprocessing
import os
import re
import shutil
import signal
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from . import __version__
from .store import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_PROCESSING,
    TaskRecord,
    TaskStore,
)
from .worker import worker_main


SUPPORTED_SUFFIXES = {
    "pdf",
    "png",
    "jpg",
    "jpeg",
    "jp2",
    "webp",
    "gif",
    "bmp",
    "tiff",
    "docx",
    "pptx",
    "xlsx",
}
ALLOWED_PARSE_METHODS = {"auto", "txt", "ocr"}
TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= minimum else default


def _output_root() -> Path:
    root = Path(
        os.getenv(
            "MINERU_API_OUTPUT_ROOT",
            "/var/lib/deftpdf-deep-parse/output",
        )
    ).expanduser()
    return root


def _database_path() -> str:
    return os.getenv(
        "DEEP_PARSE_DB_PATH",
        "/var/lib/deftpdf-deep-parse/tasks.sqlite3",
    )


def _utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _safe_filename(filename: str) -> str:
    basename = Path(filename).name.strip()
    if not basename:
        basename = f"upload-{uuid.uuid4()}.pdf"
    stem = _truncate_utf8(Path(basename).stem.strip(), 180) or "upload"
    suffix = Path(basename).suffix.lower()
    return f"{stem}{suffix}"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError as exc:
            truncated = truncated[: exc.start]
    return ""


def _unique_stems(filenames: list[str]) -> list[str]:
    used: set[str] = set()
    result: list[str] = []
    for filename in filenames:
        base = _truncate_utf8(Path(filename).stem.strip(), 180) or "document"
        candidate = base
        counter = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{counter}"
            counter += 1
        used.add(candidate.casefold())
        result.append(candidate)
    return result


@dataclass(frozen=True)
class SubmittedOptions:
    backend: str
    parse_method: str
    lang_list: list[str]
    formula_enable: bool
    table_enable: bool
    server_url: str | None
    return_md: bool
    return_middle_json: bool
    return_model_output: bool
    return_content_list: bool
    return_images: bool
    response_format_zip: bool
    return_original_file: bool
    start_page_id: int
    end_page_id: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "parse_method": self.parse_method,
            "lang_list": self.lang_list,
            "formula_enable": self.formula_enable,
            "table_enable": self.table_enable,
            "server_url": self.server_url,
            "return_md": self.return_md,
            "return_middle_json": self.return_middle_json,
            "return_model_output": self.return_model_output,
            "return_content_list": self.return_content_list,
            "return_images": self.return_images,
            "response_format_zip": self.response_format_zip,
            "return_original_file": self.return_original_file,
            "start_page_id": self.start_page_id,
            "end_page_id": self.end_page_id,
        }


class ServiceRuntime:
    def __init__(
        self,
        database_path: str | None = None,
        *,
        worker_enabled: bool = True,
        output_root: str | None = None,
    ):
        self.store = TaskStore(database_path or _database_path())
        self.worker_enabled = worker_enabled
        self.output_root = (
            Path(output_root).expanduser()
            if output_root is not None
            else _output_root()
        )
        self.worker: multiprocessing.Process | None = None
        self.stopping = False
        self.watchdog_task: asyncio.Task[Any] | None = None
        self.cleanup_task: asyncio.Task[Any] | None = None
        self.last_worker_error: str | None = None
        self.max_task_runtime_seconds = _env_int(
            "DEEP_PARSE_TASK_TIMEOUT_SECONDS",
            7200,
            minimum=300,
        )
        self.max_worker_attempts = _env_int(
            "DEEP_PARSE_MAX_WORKER_ATTEMPTS",
            3,
            minimum=1,
        )
        self.task_retention_seconds = _env_int(
            "MINERU_API_TASK_RETENTION_SECONDS",
            86400,
            minimum=0,
        )
        self.cleanup_interval_seconds = _env_int(
            "MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS",
            300,
            minimum=1,
        )

    async def start(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.output_root = self.output_root.resolve()
        self.store.initialize()
        recovered, failed = self.store.recover_processing_tasks_after_crash(
            "Deep Parse service restarted after an interrupted worker",
            self.max_worker_attempts,
        )
        if recovered or failed:
            self.last_worker_error = (
                f"Recovered {recovered} interrupted task(s); "
                f"failed {failed} crash-loop task(s)"
            )
        self.stopping = False
        if self.worker_enabled:
            self._spawn_worker()
            self.watchdog_task = asyncio.create_task(self._watchdog_loop())
        if self.task_retention_seconds > 0:
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        self.stopping = True
        for task in (self.watchdog_task, self.cleanup_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in (self.watchdog_task, self.cleanup_task) if task],
            return_exceptions=True,
        )
        self.watchdog_task = None
        self.cleanup_task = None
        self._stop_worker(grace_seconds=5)
        self.store.recover_processing_tasks(
            "Deep Parse service stopped before the worker finished"
        )

    def healthy(self) -> bool:
        if not self.worker_enabled:
            return True
        return self.worker is not None and self.worker.is_alive()

    def worker_pid(self) -> int | None:
        if self.worker is None or not self.worker.is_alive():
            return None
        return self.worker.pid

    def _spawn_worker(self) -> None:
        context = multiprocessing.get_context("spawn")
        self.worker = context.Process(
            target=worker_main,
            args=(
                self.store.database_path,
                _env_float("DEEP_PARSE_WORKER_POLL_SECONDS", 0.5, minimum=0.1),
            ),
            name="deftpdf-deep-parse-worker",
            daemon=False,
        )
        self.worker.start()

    def _stop_worker(self, grace_seconds: int = 0) -> None:
        worker = self.worker
        self.worker = None
        if worker is None:
            return
        if worker.is_alive():
            try:
                os.killpg(worker.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                worker.terminate()
            worker.join(timeout=max(0, grace_seconds))
        if worker.is_alive():
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                worker.kill()
            worker.join(timeout=5)
        else:
            worker.join(timeout=1)

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                if self.stopping:
                    return
                worker = self.worker
                if worker is None or not worker.is_alive():
                    exit_code = worker.exitcode if worker is not None else None
                    self.last_worker_error = (
                        f"Worker exited unexpectedly with code {exit_code}"
                    )
                    self._stop_worker()
                    recovered, failed = (
                        self.store.recover_processing_tasks_after_crash(
                            self.last_worker_error,
                            self.max_worker_attempts,
                        )
                    )
                    if failed:
                        self.last_worker_error += (
                            f"; failed {failed} task(s) after "
                            f"{self.max_worker_attempts} worker attempts"
                        )
                    self._spawn_worker()
                    continue

                processing = self.store.current_processing()
                started_at = _utc_timestamp(
                    processing.started_at if processing else None
                )
                if processing is None or started_at is None:
                    continue
                elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                if elapsed <= self.max_task_runtime_seconds:
                    continue

                task_id = processing.task_id
                self.last_worker_error = (
                    f"Task {task_id} exceeded {self.max_task_runtime_seconds} seconds"
                )
                self._stop_worker()
                current = self.store.get(task_id)
                if current is not None and current.status == TASK_PROCESSING:
                    self.store.fail(
                        task_id,
                        "MinerU parsing exceeded the configured hard timeout.",
                        "TASK_TIMEOUT",
                    )
                self.store.recover_processing_tasks(
                    "Worker restarted after another task exceeded its hard timeout"
                )
                self._spawn_worker()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_worker_error = str(exc)
            raise

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval_seconds)
                cutoff = datetime.now(timezone.utc) - timedelta(
                    seconds=self.task_retention_seconds
                )
                for task in self.store.expired_terminal_tasks(cutoff.isoformat()):
                    if not self.store.delete(task.task_id):
                        continue
                    shutil.rmtree(task.output_dir, ignore_errors=True)
        except asyncio.CancelledError:
            raise


def _task_payload(task: TaskRecord, request: Request, runtime: ServiceRuntime) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task.task_id,
        "status": task.status,
        "backend": task.backend,
        "file_names": task.file_names,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "error": task.error,
        "error_code": task.error_code,
        "attempts": task.attempt_count,
        "recovery_count": task.recovery_count,
        "status_url": str(request.url_for("get_async_task_status", task_id=task.task_id)),
        "result_url": str(request.url_for("get_async_task_result", task_id=task.task_id)),
    }
    if task.status == TASK_PENDING:
        payload["queued_ahead"] = runtime.store.queued_ahead(task)
    return payload


async def _save_uploads(
    task_dir: Path,
    uploads: list[UploadFile],
) -> tuple[list[str], list[str], list[str]]:
    upload_dir = task_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    max_upload_bytes = _env_int(
        "DEEP_PARSE_MAX_UPLOAD_BYTES",
        536870912,
        minimum=1,
    )
    saved_paths: list[str] = []
    original_names: list[str] = []
    normalized_names: list[str] = []

    try:
        for index, upload in enumerate(uploads):
            original_name = upload.filename or f"upload-{index + 1}.pdf"
            normalized = _safe_filename(original_name)
            suffix = Path(normalized).suffix.lower().lstrip(".")
            if suffix not in SUPPORTED_SUFFIXES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported file type: {suffix or 'unknown'}",
                )
            destination = upload_dir / f"{index + 1:03d}-{normalized}"
            total = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = await upload.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="Uploaded file exceeds the Deep Parse service limit",
                        )
                    handle.write(chunk)
            if total <= 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")
            saved_paths.append(str(destination))
            original_names.append(original_name)
            normalized_names.append(normalized)
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise
    finally:
        for upload in uploads:
            await upload.close()

    return original_names, _unique_stems(normalized_names), saved_paths


def _include_result_path(path: Path, task: TaskRecord) -> bool:
    if not path.is_file():
        return False
    if "uploads" in path.parts:
        return False
    name = path.name.lower()
    options = task.options
    if name.endswith(".md"):
        return bool(options.get("return_md", True))
    if name.endswith("_middle.json"):
        return bool(options.get("return_middle_json", False))
    if name.endswith("_model.json"):
        return bool(options.get("return_model_output", False))
    if "_content_list" in name and name.endswith(".json"):
        return bool(options.get("return_content_list", False))
    if "images" in path.parts:
        return bool(options.get("return_images", False))
    if "_origin." in name:
        return bool(options.get("return_original_file", False))
    return False


def _create_result_zip(task: TaskRecord) -> Path:
    output_dir = Path(task.output_dir)
    zip_path = output_dir / f".{task.task_id}-result.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if _include_result_path(path, task):
                archive.write(path, path.relative_to(output_dir).as_posix())
    if not zip_path.is_file() or zip_path.stat().st_size <= 22:
        zip_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=409,
            detail="Task completed without a readable result",
        )
    return zip_path


def _cleanup_result_zip(path: str) -> None:
    Path(path).unlink(missing_ok=True)


def _build_result_response(
    task: TaskRecord,
    background_tasks: BackgroundTasks,
) -> Response:
    if bool(task.options.get("response_format_zip", False)):
        zip_path = _create_result_zip(task)
        background_tasks.add_task(_cleanup_result_zip, str(zip_path))
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"{task.task_id}.zip",
        )

    results: dict[str, dict[str, Any]] = {}
    output_dir = Path(task.output_dir)
    for file_name in task.file_names:
        item: dict[str, Any] = {}
        for path in output_dir.rglob(f"{file_name}.md"):
            item["md_content"] = path.read_text(encoding="utf-8", errors="replace")
            break
        results[file_name] = item
    return JSONResponse(
        {
            "task_id": task.task_id,
            "status": task.status,
            "backend": task.backend,
            "version": __version__,
            "results": results,
        }
    )


def create_app(runtime: ServiceRuntime | None = None) -> FastAPI:
    service_runtime = runtime or ServiceRuntime()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await service_runtime.start()
        try:
            yield
        finally:
            await service_runtime.stop()

    enable_docs = os.getenv("MINERU_API_ENABLE_FASTAPI_DOCS", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    api = FastAPI(
        title="DeftPDF Deep Parse Service",
        version=__version__,
        openapi_url="/openapi.json" if enable_docs else None,
        docs_url="/docs" if enable_docs else None,
        redoc_url="/redoc" if enable_docs else None,
        lifespan=lifespan,
    )
    api.state.runtime = service_runtime

    async def submit_task(
        request: Request,
        files: Annotated[list[UploadFile], File(...)],
        lang_list: Annotated[list[str], Form()] = ["ch"],
        backend: Annotated[str, Form()] = "pipeline",
        parse_method: Annotated[str, Form()] = "auto",
        formula_enable: Annotated[bool, Form()] = True,
        table_enable: Annotated[bool, Form()] = True,
        server_url: Annotated[str | None, Form()] = None,
        return_md: Annotated[bool, Form()] = True,
        return_middle_json: Annotated[bool, Form()] = False,
        return_model_output: Annotated[bool, Form()] = False,
        return_content_list: Annotated[bool, Form()] = False,
        return_images: Annotated[bool, Form()] = False,
        response_format_zip: Annotated[bool, Form()] = False,
        return_original_file: Annotated[bool, Form()] = False,
        start_page_id: Annotated[int, Form()] = 0,
        end_page_id: Annotated[int, Form()] = 99999,
        idempotency_key: Annotated[str | None, Form()] = None,
    ) -> tuple[TaskRecord, dict[str, Any]]:
        if not files:
            raise HTTPException(status_code=400, detail="No files were uploaded")
        if parse_method not in ALLOWED_PARSE_METHODS:
            raise HTTPException(status_code=400, detail="Invalid parse_method")
        normalized_idempotency_key = (
            str(idempotency_key).strip().lower() if idempotency_key else ""
        )
        if normalized_idempotency_key and not re.fullmatch(
            r"[0-9a-f]{64}",
            normalized_idempotency_key,
        ):
            raise HTTPException(status_code=400, detail="Invalid idempotency_key")
        if normalized_idempotency_key:
            existing = service_runtime.store.get_by_idempotency_key(
                normalized_idempotency_key
            )
            if existing is not None:
                return (
                    existing,
                    _task_payload(existing, request, service_runtime),
                )
        task_id = str(uuid.uuid4())
        task_dir = service_runtime.output_root / task_id
        original_names, file_names, upload_paths = await _save_uploads(task_dir, files)
        options = SubmittedOptions(
            backend=backend,
            parse_method=parse_method,
            lang_list=lang_list or ["ch"],
            formula_enable=formula_enable,
            table_enable=table_enable,
            server_url=server_url,
            return_md=return_md,
            return_middle_json=return_middle_json,
            return_model_output=return_model_output,
            return_content_list=return_content_list,
            return_images=return_images,
            response_format_zip=response_format_zip,
            return_original_file=return_original_file and response_format_zip,
            start_page_id=max(0, start_page_id),
            end_page_id=max(0, end_page_id),
        )
        try:
            task = service_runtime.store.create_task(
                task_id=task_id,
                idempotency_key=normalized_idempotency_key or None,
                backend=backend,
                file_names=file_names,
                output_dir=str(task_dir),
                options=options.as_dict(),
                upload_names=original_names,
                uploads=upload_paths,
            )
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
        if task.task_id != task_id:
            shutil.rmtree(task_dir, ignore_errors=True)
        return task, _task_payload(task, request, service_runtime)

    @api.post("/tasks", status_code=202)
    async def submit_parse_task(
        request: Request,
        files: Annotated[list[UploadFile], File(...)],
        lang_list: Annotated[list[str], Form()] = ["ch"],
        backend: Annotated[str, Form()] = "pipeline",
        parse_method: Annotated[str, Form()] = "auto",
        formula_enable: Annotated[bool, Form()] = True,
        table_enable: Annotated[bool, Form()] = True,
        server_url: Annotated[str | None, Form()] = None,
        return_md: Annotated[bool, Form()] = True,
        return_middle_json: Annotated[bool, Form()] = False,
        return_model_output: Annotated[bool, Form()] = False,
        return_content_list: Annotated[bool, Form()] = False,
        return_images: Annotated[bool, Form()] = False,
        response_format_zip: Annotated[bool, Form()] = False,
        return_original_file: Annotated[bool, Form()] = False,
        start_page_id: Annotated[int, Form()] = 0,
        end_page_id: Annotated[int, Form()] = 99999,
        idempotency_key: Annotated[str | None, Form()] = None,
    ):
        task, payload = await submit_task(
            request,
            files,
            lang_list,
            backend,
            parse_method,
            formula_enable,
            table_enable,
            server_url,
            return_md,
            return_middle_json,
            return_model_output,
            return_content_list,
            return_images,
            response_format_zip,
            return_original_file,
            start_page_id,
            end_page_id,
            idempotency_key,
        )
        payload["message"] = "Task submitted successfully"
        return JSONResponse(status_code=202, content=payload)

    @api.post("/file_parse")
    async def parse_file_synchronously(
        request: Request,
        background_tasks: BackgroundTasks,
        files: Annotated[list[UploadFile], File(...)],
        lang_list: Annotated[list[str], Form()] = ["ch"],
        backend: Annotated[str, Form()] = "pipeline",
        parse_method: Annotated[str, Form()] = "auto",
        formula_enable: Annotated[bool, Form()] = True,
        table_enable: Annotated[bool, Form()] = True,
        server_url: Annotated[str | None, Form()] = None,
        return_md: Annotated[bool, Form()] = True,
        return_middle_json: Annotated[bool, Form()] = False,
        return_model_output: Annotated[bool, Form()] = False,
        return_content_list: Annotated[bool, Form()] = False,
        return_images: Annotated[bool, Form()] = False,
        response_format_zip: Annotated[bool, Form()] = False,
        return_original_file: Annotated[bool, Form()] = False,
        start_page_id: Annotated[int, Form()] = 0,
        end_page_id: Annotated[int, Form()] = 99999,
        idempotency_key: Annotated[str | None, Form()] = None,
    ):
        task, _payload = await submit_task(
            request,
            files,
            lang_list,
            backend,
            parse_method,
            formula_enable,
            table_enable,
            server_url,
            return_md,
            return_middle_json,
            return_model_output,
            return_content_list,
            return_images,
            response_format_zip,
            return_original_file,
            start_page_id,
            end_page_id,
            idempotency_key,
        )
        while True:
            await asyncio.sleep(1)
            current = service_runtime.store.get(task.task_id)
            if current is None:
                raise HTTPException(status_code=404, detail="Task not found")
            if current.status == TASK_FAILED:
                return JSONResponse(
                    status_code=409,
                    content={
                        **_task_payload(current, request, service_runtime),
                        "message": "Task execution failed",
                    },
                )
            if current.status == TASK_COMPLETED:
                response = _build_result_response(current, background_tasks)
                response.headers["X-MinerU-Task-Id"] = current.task_id
                response.headers["X-MinerU-Task-Status"] = current.status
                return response

    @api.get("/tasks/{task_id}", name="get_async_task_status")
    async def get_async_task_status(task_id: str, request: Request):
        if not TASK_ID_PATTERN.match(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        task = service_runtime.store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return _task_payload(task, request, service_runtime)

    @api.get("/tasks/{task_id}/result", name="get_async_task_result")
    async def get_async_task_result(
        task_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        if not TASK_ID_PATTERN.match(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        task = service_runtime.store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status in {TASK_PENDING, TASK_PROCESSING}:
            return JSONResponse(
                status_code=202,
                content={
                    **_task_payload(task, request, service_runtime),
                    "message": "Task result is not ready yet",
                },
            )
        if task.status == TASK_FAILED:
            return JSONResponse(
                status_code=409,
                content={
                    **_task_payload(task, request, service_runtime),
                    "message": "Task execution failed",
                },
            )
        return _build_result_response(task, background_tasks)

    @api.get("/health")
    async def health():
        counts = service_runtime.store.counts()
        healthy = service_runtime.healthy()
        payload = {
            "status": "healthy" if healthy else "unhealthy",
            "service_version": __version__,
            "version": "3.0.9",
            "protocol_version": 2,
            "persistent_tasks": True,
            "queued_tasks": counts[TASK_PENDING],
            "processing_tasks": counts[TASK_PROCESSING],
            "completed_tasks": counts[TASK_COMPLETED],
            "failed_tasks": counts[TASK_FAILED],
            "max_concurrent_requests": 1,
            "worker_pid": service_runtime.worker_pid(),
            "max_task_runtime_seconds": service_runtime.max_task_runtime_seconds,
            "max_worker_attempts": service_runtime.max_worker_attempts,
            "task_retention_seconds": service_runtime.task_retention_seconds,
            "task_cleanup_interval_seconds": service_runtime.cleanup_interval_seconds,
            "last_worker_error": service_runtime.last_worker_error,
        }
        return JSONResponse(status_code=200 if healthy else 503, content=payload)

    return api


app = create_app()
