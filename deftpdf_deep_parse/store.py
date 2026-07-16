from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


TASK_PENDING = "pending"
TASK_PROCESSING = "processing"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_TERMINAL = {TASK_COMPLETED, TASK_FAILED}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    idempotency_key: str | None
    status: str
    backend: str
    file_names: list[str]
    created_at: str
    output_dir: str
    options: dict[str, Any]
    upload_names: list[str]
    uploads: list[str]
    submit_order: int
    started_at: str | None
    completed_at: str | None
    error: str | None
    error_code: str | None
    worker_pid: int | None
    attempt_count: int
    recovery_count: int


class TaskStore:
    def __init__(self, database_path: str):
        self.database_path = str(Path(database_path).expanduser())

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        database = Path(self.database_path)
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    file_names_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    upload_names_json TEXT NOT NULL,
                    uploads_json TEXT NOT NULL,
                    submit_order INTEGER NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT,
                    error_code TEXT,
                    worker_pid INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_order
                    ON tasks(status, submit_order);

                CREATE INDEX IF NOT EXISTS idx_tasks_completed_at
                    ON tasks(status, completed_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "idempotency_key" not in columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN idempotency_key TEXT"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency_key
                    ON tasks(idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                """
            )

    def create_task(
        self,
        *,
        task_id: str,
        idempotency_key: str | None = None,
        backend: str,
        file_names: list[str],
        output_dir: str,
        options: dict[str, Any],
        upload_names: list[str],
        uploads: list[str],
    ) -> TaskRecord:
        created_at = utc_now_iso()
        created = False
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            next_order = int(
                connection.execute(
                    "SELECT COALESCE(MAX(submit_order), 0) + 1 FROM tasks"
                ).fetchone()[0]
            )
            try:
                connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, idempotency_key, status, backend,
                        file_names_json, created_at, output_dir, options_json,
                        upload_names_json, uploads_json, submit_order
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        idempotency_key,
                        TASK_PENDING,
                        backend,
                        json.dumps(file_names, ensure_ascii=False),
                        created_at,
                        output_dir,
                        json.dumps(options, ensure_ascii=False),
                        json.dumps(upload_names, ensure_ascii=False),
                        json.dumps(uploads, ensure_ascii=False),
                        next_order,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
            else:
                connection.execute("COMMIT")
                created = True
        if not created and idempotency_key:
            existing = self.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
        if not created:
            raise RuntimeError("Failed to persist Deep Parse task")
        record = self.get(task_id)
        if record is None:
            raise RuntimeError("Failed to persist Deep Parse task")
        return record

    def get_by_idempotency_key(self, idempotency_key: str) -> TaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def get(self, task_id: str) -> TaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def claim_next_pending(self, worker_pid: int) -> TaskRecord | None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id
                FROM tasks
                WHERE status = ?
                ORDER BY submit_order ASC
                LIMIT 1
                """,
                (TASK_PENDING,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None

            task_id = str(row["task_id"])
            connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    started_at = ?,
                    completed_at = NULL,
                    error = NULL,
                    error_code = NULL,
                    worker_pid = ?,
                    attempt_count = attempt_count + 1
                WHERE task_id = ? AND status = ?
                """,
                (
                    TASK_PROCESSING,
                    utc_now_iso(),
                    int(worker_pid),
                    task_id,
                    TASK_PENDING,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            connection.execute("COMMIT")
        return self._record(claimed) if claimed is not None else None

    def complete(self, task_id: str) -> None:
        self._finish(task_id, TASK_COMPLETED, None, None)

    def fail(self, task_id: str, error: str, error_code: str = "PARSE_FAILED") -> None:
        self._finish(task_id, TASK_FAILED, error, error_code)

    def _finish(
        self,
        task_id: str,
        status: str,
        error: str | None,
        error_code: str | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    completed_at = ?,
                    error = ?,
                    error_code = ?,
                    worker_pid = NULL
                WHERE task_id = ?
                """,
                (
                    status,
                    utc_now_iso(),
                    (error or "")[:4000] or None,
                    error_code,
                    task_id,
                ),
            )

    def recover_processing_tasks(self, reason: str) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    started_at = NULL,
                    completed_at = NULL,
                    error = ?,
                    error_code = 'WORKER_RECOVERED',
                    worker_pid = NULL,
                    recovery_count = recovery_count + 1
                WHERE status = ?
                """,
                (
                    TASK_PENDING,
                    reason[:4000],
                    TASK_PROCESSING,
                ),
            )
            return int(cursor.rowcount)

    def recover_processing_tasks_after_crash(
        self,
        reason: str,
        max_attempts: int,
    ) -> tuple[int, int]:
        max_attempts = max(1, int(max_attempts))
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            failed = connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    completed_at = ?,
                    error = ?,
                    error_code = 'WORKER_CRASH_LOOP',
                    worker_pid = NULL,
                    recovery_count = recovery_count + 1
                WHERE status = ?
                  AND attempt_count >= ?
                """,
                (
                    TASK_FAILED,
                    utc_now_iso(),
                    (
                        "MinerU worker exited repeatedly while processing this "
                        "document."
                    ),
                    TASK_PROCESSING,
                    max_attempts,
                ),
            )
            recovered = connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    started_at = NULL,
                    completed_at = NULL,
                    error = ?,
                    error_code = 'WORKER_RECOVERED',
                    worker_pid = NULL,
                    recovery_count = recovery_count + 1
                WHERE status = ?
                """,
                (
                    TASK_PENDING,
                    reason[:4000],
                    TASK_PROCESSING,
                ),
            )
            connection.execute("COMMIT")
        return int(recovered.rowcount), int(failed.rowcount)

    def queued_ahead(self, task: TaskRecord) -> int:
        if task.status != TASK_PENDING:
            return 0
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE submit_order < ?
                  AND status IN (?, ?)
                """,
                (
                    task.submit_order,
                    TASK_PENDING,
                    TASK_PROCESSING,
                ),
            ).fetchone()
        return int(row[0] if row is not None else 0)

    def counts(self) -> dict[str, int]:
        counts = {
            TASK_PENDING: 0,
            TASK_PROCESSING: 0,
            TASK_COMPLETED: 0,
            TASK_FAILED: 0,
        }
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM tasks GROUP BY status"
            ).fetchall()
        for row in rows:
            status = str(row["status"])
            if status in counts:
                counts[status] = int(row["total"])
        return counts

    def current_processing(self) -> TaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM tasks
                WHERE status = ?
                ORDER BY started_at ASC
                LIMIT 1
                """,
                (TASK_PROCESSING,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def expired_terminal_tasks(self, cutoff_iso: str) -> list[TaskRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM tasks
                WHERE status IN (?, ?)
                  AND completed_at IS NOT NULL
                  AND completed_at <= ?
                ORDER BY completed_at ASC
                """,
                (TASK_COMPLETED, TASK_FAILED, cutoff_iso),
            ).fetchall()
        return [self._record(row) for row in rows]

    def delete(self, task_id: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM tasks
                WHERE task_id = ?
                  AND status IN (?, ?)
                """,
                (task_id, TASK_COMPLETED, TASK_FAILED),
            )
            return int(cursor.rowcount) > 0

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]),
            idempotency_key=(
                str(row["idempotency_key"])
                if "idempotency_key" in row.keys() and row["idempotency_key"]
                else None
            ),
            status=str(row["status"]),
            backend=str(row["backend"]),
            file_names=list(json.loads(str(row["file_names_json"]))),
            created_at=str(row["created_at"]),
            output_dir=str(row["output_dir"]),
            options=dict(json.loads(str(row["options_json"]))),
            upload_names=list(json.loads(str(row["upload_names_json"]))),
            uploads=list(json.loads(str(row["uploads_json"]))),
            submit_order=int(row["submit_order"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,
            completed_at=str(row["completed_at"]) if row["completed_at"] else None,
            error=str(row["error"]) if row["error"] else None,
            error_code=str(row["error_code"]) if row["error_code"] else None,
            worker_pid=int(row["worker_pid"]) if row["worker_pid"] else None,
            attempt_count=int(row["attempt_count"]),
            recovery_count=int(row["recovery_count"]),
        )
