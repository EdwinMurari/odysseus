"""The v1 capability-worker HTTP protocol, single-sourced.

Odysseus's ``capability_runner`` consumes exactly this contract:

- ``POST   /v1/runs``            -> ``{"id", "status": "queued"}``
- ``GET    /v1/runs/{id}``       -> ``{"id", "status", "result", "error", ...}``
- ``GET    /v1/runs/{id}/log``   -> ``{"id", "output": "<tail>"}``
- ``DELETE /v1/runs/{id}``       -> cancel; idempotent on terminal runs
- ``GET    /v1/readiness``       -> ``{"ready", "dependencies": [...]}``
- ``GET    /healthz``            -> unauthenticated liveness

Workers provide a :class:`RunExecutor` (how work actually runs: subprocess,
thread, ...) and a readiness provider; the protocol surface, auth, and the
cancel/finish race are handled here once.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from capability_kit.auth import verify_bearer
from capability_kit.runs import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    RunStore,
    new_run_record,
)

MAX_LOG_TAIL = 5000
DEFAULT_LOG_TAIL = 400


class RunCreate(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    odysseus_run_id: str | None = None


class RunExecutor(Protocol):
    """How a worker actually executes a run. The router owns everything else."""

    def validate(self, raw_input: dict[str, Any]) -> None:
        """Reject bad input with HTTPException(4xx) before a run is created."""

    def start(
        self, run_id: str, raw_input: dict[str, Any], odysseus_run_id: str | None
    ) -> None:
        """Launch the run asynchronously (thread, subprocess task, ...)."""

    def cancel(self, run_id: str) -> None:
        """Best-effort termination; the record is already marked cancelled."""

    def read_log(self, run_id: str, tail: int) -> str:
        """Return the last ``tail`` lines of the run's log."""


def bearer_guard(token_provider: Callable[[], str]) -> Callable[[Request], None]:
    """FastAPI dependency enforcing constant-time bearer auth.

    A worker without a configured token answers 503 — it must surface as
    not-ready in the Odysseus UI rather than accept unauthenticated calls.
    """

    def guard(request: Request) -> None:
        expected = (token_provider() or "").strip()
        if not expected:
            raise HTTPException(503, "capability worker has no auth token configured")
        if not verify_bearer(request.headers.get("authorization"), expected):
            raise HTTPException(401, "invalid bearer token")

    return guard


def clamp_tail(tail: int) -> int:
    return max(1, min(tail, MAX_LOG_TAIL))


def tail_lines(text: str, tail: int) -> str:
    return "\n".join(text.splitlines()[-clamp_tail(tail):])


def run_view(record: dict[str, Any]) -> dict[str, Any]:
    """The status payload Odysseus polls. ``input`` stays internal."""
    return {
        "id": record["id"],
        "status": record["status"],
        "odysseus_run_id": record.get("odysseus_run_id"),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "result": record.get("result") or {},
        "error": record.get("error"),
    }


def env_dependency(
    dep_id: str,
    env_names: list[str] | str,
    missing_message: str,
    ready_message: str = "Available",
) -> dict[str, Any]:
    """Availability of one integration, judged by env presence only.

    Never echoes values — the detail strings are fixed messages.
    """
    names = [env_names] if isinstance(env_names, str) else list(env_names)
    available = bool(names) and all(bool(os.environ.get(str(n))) for n in names)
    return {
        "id": dep_id,
        "available": available,
        "detail": ready_message if available else missing_message,
    }


def env_readiness(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Dependency report from a ``CAPABILITY_READINESS_JSON``-shaped mapping."""
    dependencies = []
    for dep_id, item in spec.items():
        if not isinstance(item, dict):
            continue
        dependencies.append(
            env_dependency(
                str(dep_id),
                item.get("env") or [],
                str(item.get("missing_message") or "Not configured"),
                str(item.get("ready_message") or "Available"),
            )
        )
    return dependencies


def readiness_report(dependencies: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ready": all(item.get("available") for item in dependencies),
        "dependencies": dependencies,
    }


def create_run_lifecycle_router(
    *,
    store: RunStore,
    executor: RunExecutor,
    token_provider: Callable[[], str],
    readiness_provider: Callable[[], list[dict[str, Any]]],
) -> APIRouter:
    guard = bearer_guard(token_provider)
    router = APIRouter()

    def _require_auth(request: Request) -> None:
        guard(request)

    @router.post("/v1/runs")
    def create_run(body: RunCreate, request: Request) -> dict[str, Any]:
        _require_auth(request)
        executor.validate(body.input)
        record = new_run_record(body.input, body.odysseus_run_id)
        store.create(record)
        executor.start(record["id"], body.input, body.odysseus_run_id)
        return {"id": record["id"], "status": "queued"}

    @router.get("/v1/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request)
        record = store.get(run_id)
        if record is None:
            raise HTTPException(404, "Run not found")
        return run_view(record)

    @router.get("/v1/runs/{run_id}/log")
    def get_run_log(
        run_id: str, request: Request, tail: int = DEFAULT_LOG_TAIL
    ) -> dict[str, Any]:
        _require_auth(request)
        if store.get(run_id) is None:
            raise HTTPException(404, "Run not found")
        return {"id": run_id, "output": executor.read_log(run_id, clamp_tail(tail))}

    @router.delete("/v1/runs/{run_id}")
    def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        _require_auth(request)
        record = store.get(run_id)
        if record is None:
            raise HTTPException(404, "Run not found")
        if record["status"] in TERMINAL_STATUSES:
            return {"id": run_id, "status": record["status"]}
        store.transition(
            run_id,
            ACTIVE_STATUSES,
            status="cancelled",
            error="Cancelled by Odysseus",
            finished_at=time.time(),
        )
        executor.cancel(run_id)
        return {"id": run_id, "status": "cancelled"}

    @router.get("/v1/readiness")
    def readiness(request: Request) -> dict[str, Any]:
        _require_auth(request)
        return readiness_report(readiness_provider())

    @router.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

    return router


def create_worker_app(
    *,
    title: str,
    store: RunStore,
    executor: RunExecutor,
    token_provider: Callable[[], str],
    readiness_provider: Callable[[], list[dict[str, Any]]],
) -> FastAPI:
    app = FastAPI(title=title, version="1")
    app.include_router(
        create_run_lifecycle_router(
            store=store,
            executor=executor,
            token_provider=token_provider,
            readiness_provider=readiness_provider,
        )
    )
    return app
