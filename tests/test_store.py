from pathlib import Path

from deftpdf_deep_parse.store import (
    TASK_COMPLETED,
    TASK_PENDING,
    TASK_PROCESSING,
    TaskStore,
)


def create_task(store: TaskStore, root: Path, task_id: str):
    task_dir = root / task_id
    task_dir.mkdir()
    source = task_dir / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return store.create_task(
        task_id=task_id,
        backend="pipeline",
        file_names=["source"],
        output_dir=str(task_dir),
        options={
            "backend": "pipeline",
            "parse_method": "auto",
            "return_md": True,
        },
        upload_names=["source.pdf"],
        uploads=[str(source)],
    )


def test_task_state_survives_store_reopen_and_recovery(tmp_path: Path):
    database = tmp_path / "tasks.sqlite3"
    store = TaskStore(str(database))
    store.initialize()
    created = create_task(
        store,
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
    )

    claimed = store.claim_next_pending(1234)
    assert claimed is not None
    assert claimed.task_id == created.task_id
    assert claimed.status == TASK_PROCESSING

    reopened = TaskStore(str(database))
    reopened.initialize()
    assert reopened.get(created.task_id).status == TASK_PROCESSING
    assert reopened.recover_processing_tasks("service restarted") == 1

    recovered = reopened.get(created.task_id)
    assert recovered is not None
    assert recovered.status == TASK_PENDING
    assert recovered.recovery_count == 1

    claimed_again = reopened.claim_next_pending(5678)
    assert claimed_again is not None
    assert claimed_again.attempt_count == 2
    reopened.complete(created.task_id)
    assert reopened.get(created.task_id).status == TASK_COMPLETED


def test_queue_order_and_counts_are_persistent(tmp_path: Path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.initialize()
    first = create_task(
        store,
        tmp_path,
        "22222222-2222-4222-8222-222222222222",
    )
    second = create_task(
        store,
        tmp_path,
        "33333333-3333-4333-8333-333333333333",
    )

    assert store.queued_ahead(first) == 0
    assert store.queued_ahead(second) == 1
    assert store.claim_next_pending(123).task_id == first.task_id
    refreshed_second = store.get(second.task_id)
    assert refreshed_second is not None
    assert store.queued_ahead(refreshed_second) == 1

    counts = store.counts()
    assert counts[TASK_PROCESSING] == 1
    assert counts[TASK_PENDING] == 1
