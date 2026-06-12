"""Run-record stores backing the v1 worker protocol.

Two implementations of one contract:

- ``MemoryRunStore`` — in-process, for workers whose runs die with the
  process (stock-research's thread executor).
- ``FileRunStore``  — one JSON file per run with atomic replace, for
  workers that must survive restarts (the generic subprocess worker).

``transition`` is the concurrency primitive: status changes are applied
only when the current status is in ``from_statuses``, so a finishing
executor can never overwrite a cancellation (the race the previous
copies handled ad hoc, or not at all).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Protocol

TERMINAL_STATUSES = frozenset({"success", "error", "cancelled"})
ACTIVE_STATUSES = frozenset({"queued", "running"})


def new_run_record(
    raw_input: dict[str, Any], odysseus_run_id: str | None
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "status": "queued",
        "input": raw_input,
        "odysseus_run_id": odysseus_run_id,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": {},
        "error": None,
    }


class RunStore(Protocol):
    def create(self, record: dict[str, Any]) -> None: ...

    def get(self, run_id: str) -> dict[str, Any] | None: ...

    def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None: ...

    def transition(
        self, run_id: str, from_statuses: Iterable[str], **fields: Any
    ) -> dict[str, Any] | None: ...


class MemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._runs[record["id"]] = dict(record)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            return dict(record) if record else None

    def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            record.update(fields)
            return dict(record)

    def transition(
        self, run_id: str, from_statuses: Iterable[str], **fields: Any
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record["status"] not in set(from_statuses):
                return None
            record.update(fields)
            return dict(record)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()


class FileRunStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._lock = threading.RLock()

    def _path(self, run_id: str) -> Path:
        return self._dir / f"{run_id}.json"

    def _read(self, run_id: str) -> dict[str, Any] | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, record: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record["id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(path)

    def create(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._write(dict(record))

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(run_id)

    def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            record = self._read(run_id)
            if record is None:
                return None
            record.update(fields)
            self._write(record)
            return record

    def transition(
        self, run_id: str, from_statuses: Iterable[str], **fields: Any
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self._read(run_id)
            if record is None or record["status"] not in set(from_statuses):
                return None
            record.update(fields)
            self._write(record)
            return record

    def recover_interrupted(self, error_message: str) -> int:
        """Mark runs left active by a previous process as errored (startup)."""
        recovered = 0
        with self._lock:
            for path in self._dir.glob("*.json") if self._dir.exists() else []:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if record.get("status") in ACTIVE_STATUSES:
                    record["status"] = "error"
                    record["error"] = error_message
                    record["finished_at"] = time.time()
                    self._write(record)
                    recovered += 1
        return recovered
