from pathlib import Path
from io import BytesIO
import zipfile

from fastapi.testclient import TestClient

from deftpdf_deep_parse.app import ServiceRuntime, create_app
from deftpdf_deep_parse.store import TaskStore


def test_health_reports_persistent_task_runtime(tmp_path: Path):
    runtime = ServiceRuntime(
        str(tmp_path / "tasks.sqlite3"),
        worker_enabled=False,
        output_root=str(tmp_path / "output"),
    )
    with TestClient(create_app(runtime)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["persistent_tasks"] is True
    assert payload["protocol_version"] == 2


def test_task_status_remains_available_after_app_restart(tmp_path: Path):
    database = tmp_path / "tasks.sqlite3"
    output_dir = tmp_path / "task"
    output_dir.mkdir()
    source = output_dir / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")

    store = TaskStore(str(database))
    store.initialize()
    task = store.create_task(
        task_id="44444444-4444-4444-8444-444444444444",
        backend="pipeline",
        file_names=["source"],
        output_dir=str(output_dir),
        options={"backend": "pipeline", "return_md": True},
        upload_names=["source.pdf"],
        uploads=[str(source)],
    )

    first_runtime = ServiceRuntime(
        str(database),
        worker_enabled=False,
        output_root=str(tmp_path / "output"),
    )
    with TestClient(create_app(first_runtime)) as client:
        first = client.get(f"/tasks/{task.task_id}")
        assert first.status_code == 200
        assert first.json()["status"] == "pending"

    second_runtime = ServiceRuntime(
        str(database),
        worker_enabled=False,
        output_root=str(tmp_path / "output"),
    )
    with TestClient(create_app(second_runtime)) as client:
        second = client.get(f"/tasks/{task.task_id}")

    assert second.status_code == 200
    assert second.json()["task_id"] == task.task_id
    assert second.json()["status"] == "pending"


def test_submit_and_download_completed_zip(tmp_path: Path):
    database = tmp_path / "tasks.sqlite3"
    runtime = ServiceRuntime(
        str(database),
        worker_enabled=False,
        output_root=str(tmp_path / "output"),
    )
    with TestClient(create_app(runtime)) as client:
        submitted = client.post(
            "/tasks",
            data={
                "backend": "pipeline",
                "parse_method": "auto",
                "return_md": "true",
                "response_format_zip": "true",
            },
            files={
                "files": (
                    "source.pdf",
                    b"%PDF-1.4\n%%EOF\n",
                    "application/pdf",
                ),
            },
        )
        assert submitted.status_code == 202
        task_id = submitted.json()["task_id"]
        task = runtime.store.get(task_id)
        assert task is not None
        assert Path(task.uploads[0]).is_file()

        parse_dir = Path(task.output_dir) / "source" / "auto"
        parse_dir.mkdir(parents=True)
        (parse_dir / "source.md").write_text("# converted\n", encoding="utf-8")
        runtime.store.complete(task_id)

        result = client.get(f"/tasks/{task_id}/result")

    assert result.status_code == 200
    assert result.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(BytesIO(result.content)) as archive:
        assert archive.namelist() == ["source/auto/source.md"]
        assert archive.read("source/auto/source.md") == b"# converted\n"
