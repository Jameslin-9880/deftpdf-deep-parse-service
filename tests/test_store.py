from pathlib import Path

from deftpdf_deep_parse.store import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_PROCESSING,
    TaskStore,
)


def create_task(
    store: TaskStore,
    root: Path,
    task_id: str,
    idempotency_key: str | None = None,
):
    task_dir = root / task_id
    task_dir.mkdir()
    source = task_dir / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return store.create_task(
        task_id=task_id,
        idempotency_key=idempotency_key,
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


def test_idempotency_key_returns_existing_task(tmp_path: Path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.initialize()
    idempotency_key = "a" * 64
    first = create_task(
        store,
        tmp_path,
        "55555555-5555-4555-8555-555555555555",
        idempotency_key,
    )
    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    duplicate_source = duplicate_root / "source.pdf"
    duplicate_source.write_bytes(b"%PDF-1.4\n%%EOF\n")

    duplicate = store.create_task(
        task_id="66666666-6666-4666-8666-666666666666",
        idempotency_key=idempotency_key,
        backend="pipeline",
        file_names=["source"],
        output_dir=str(duplicate_root),
        options={"backend": "pipeline"},
        upload_names=["source.pdf"],
        uploads=[str(duplicate_source)],
    )

    assert duplicate.task_id == first.task_id
    assert store.get_by_idempotency_key(idempotency_key).task_id == first.task_id


def test_crash_loop_task_fails_and_next_task_can_run(tmp_path: Path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.initialize()
    poison = create_task(
        store,
        tmp_path,
        "77777777-7777-4777-8777-777777777777",
    )
    next_task = create_task(
        store,
        tmp_path,
        "88888888-8888-4888-8888-888888888888",
    )

    for attempt in range(1, 4):
        claimed = store.claim_next_pending(1000 + attempt)
        assert claimed is not None
        assert claimed.task_id == poison.task_id
        recovered, failed = store.recover_processing_tasks_after_crash(
            "worker crashed",
            max_attempts=3,
        )
        if attempt < 3:
            assert (recovered, failed) == (1, 0)
        else:
            assert (recovered, failed) == (0, 1)

    failed_poison = store.get(poison.task_id)
    assert failed_poison is not None
    assert failed_poison.status == TASK_FAILED
    assert failed_poison.error_code == "WORKER_CRASH_LOOP"
    claimed_next = store.claim_next_pending(2000)
    assert claimed_next is not None
    assert claimed_next.task_id == next_task.task_id
