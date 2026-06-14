"""Durable execution manager for manifest-defined capabilities."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from core.atomic_io import atomic_write_json
from core.database import (
    CapabilityRun,
    CapabilityAdminState,
    CapabilityPreference,
    Document,
    DocumentVersion,
    ScheduledTask,
    SessionLocal,
)
from core.platform_compat import detached_popen_kwargs, kill_process_tree, pid_alive
from src.capabilities import (
    CapabilityConfigError,
    CapabilityDefinition,
    CapabilityRegistry,
    dumps_input,
)
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

_RUNS_DIR = Path(DATA_DIR) / "capability_runs"
_TERMINAL = {"success", "error", "cancelled", "interrupted"}
_POLL_SECONDS = 2.0
try:
    _REPORT_MAX_BYTES = int(
        os.environ.get("ODYSSEUS_CAPABILITY_REPORT_MAX_BYTES", "10485760")
    )
except ValueError:
    _REPORT_MAX_BYTES = 10485760
_manager: "CapabilityManager | None" = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_to_dict(run: CapabilityRun, include_result: bool = True) -> dict[str, Any]:
    result_payload = _json_dict(run.result_json)
    warnings = result_payload.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    outcome = result_payload.get("outcome") if run.status in _TERMINAL else None
    if run.status in _TERMINAL and not outcome:
        metrics = result_payload.get("metrics") or {}
        finding_keys = ("findings", "clusters", "results", "matches")
        has_zero_findings = any(
            key in metrics and metrics.get(key) == 0 for key in finding_keys
        )
        if run.status == "success" and warnings:
            outcome = "partial"
        elif run.status == "success" and has_zero_findings:
            outcome = "no_findings"
        elif run.status == "success":
            outcome = "success"
        elif run.status in {"cancelled", "interrupted"}:
            outcome = run.status
        else:
            outcome = "error"
    result = {
        "id": run.id,
        "owner": run.owner,
        "capability_id": run.capability_id,
        "transport": run.transport,
        "status": run.status,
        "input": _json_dict(run.input_json),
        "summary": run.summary,
        "error": run.error,
        "report_path": run.report_path,
        "document_id": run.document_id,
        "provider_run_id": run.provider_run_id,
        "scheduled_task_id": run.scheduled_task_id,
        "task_run_id": run.task_run_id,
        "started_at": run.started_at.isoformat() + "Z" if run.started_at else None,
        "finished_at": run.finished_at.isoformat() + "Z" if run.finished_at else None,
        "created_at": run.created_at.isoformat() + "Z" if run.created_at else None,
        "outcome": outcome,
        "progress": result_payload.get("progress") or {},
        "metrics": result_payload.get("metrics") or {},
        "warnings": warnings,
        "failures": result_payload.get("failures") or result_payload.get("source_failures") or {},
        "provenance": result_payload.get("provenance") or result_payload.get("model_provenance") or [],
    }
    if include_result:
        result["result"] = result_payload
    return result


class CapabilityManager:
    def __init__(self, registry: CapabilityRegistry | None = None):
        self.registry = registry or CapabilityRegistry()
        self._tasks: dict[str, asyncio.Task] = {}
        self._run_definitions: dict[str, CapabilityDefinition] = {}
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        _RUNS_DIR.mkdir(parents=True, exist_ok=True)
        db = SessionLocal()
        try:
            active_ids = [
                row[0]
                for row in db.query(CapabilityRun.id)
                .filter(CapabilityRun.status.in_(("queued", "running")))
                .all()
            ]
        finally:
            db.close()
        for run_id in active_ids:
            self._spawn_driver(run_id, resume=True)
        if active_ids:
            logger.info("Recovered %d capability run(s)", len(active_ids))

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def list_definitions(self) -> list[dict[str, Any]]:
        return [self.get_definition(item.id).public_dict() for item in self.registry.list()]

    async def readiness(
        self,
        definition: CapabilityDefinition,
        owner: str | None,
        raw_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from src.endpoint_resolver import resolve_endpoint

        checks: list[dict[str, Any]] = []
        worker_dependencies: dict[str, dict[str, Any]] = {}
        worker_error = ""
        if definition.transport == "http":
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get(
                        self._url(definition, definition.readiness_path),
                        headers=self._headers(definition),
                    )
                    response.raise_for_status()
                    payload = response.json()
                    worker_dependencies = {
                        str(item.get("id")): item
                        for item in (payload.get("dependencies") or [])
                        if isinstance(item, dict) and item.get("id")
                    }
            except Exception as exc:
                worker_error = f"{type(exc).__name__}: {exc}"
        for role_name in definition.model_roles:
            try:
                role = self.registry.get_model_role(role_name)
                url, model, _headers = resolve_endpoint(role.setting_prefix, owner=owner)
                ready = bool(role.enabled and url and model)
                checks.append({
                    "id": f"model:{role_name}",
                    "kind": "model",
                    "label": role_name,
                    "ready": ready,
                    "detail": (
                        f"{model} via {role.setting_prefix}"
                        if ready
                        else f"Configure an enabled endpoint and model for {role.setting_prefix}"
                    ),
                })
            except Exception as exc:
                checks.append({
                    "id": f"model:{role_name}",
                    "kind": "model",
                    "label": role_name,
                    "ready": False,
                    "detail": str(exc),
                })
        for dependency in definition.dependencies:
            dep_id = str(dependency.get("id"))
            env_name = str(dependency.get("env") or "")
            selector = str(dependency.get("input") or "")
            selected = raw_input.get(selector) if selector and raw_input else None
            selected_values = (
                selected
                if isinstance(selected, list)
                else [item.strip() for item in str(selected or "").split(",") if item.strip()]
            )
            dependency_values = {
                str(item) for item in (dependency.get("values") or [])
            }
            applies = not selector or not dependency_values or bool(
                dependency_values.intersection(str(item) for item in selected_values)
            )
            required = bool(dependency.get("required", True)) or bool(
                applies and dependency.get("required_when_selected", False)
            )
            worker_check = worker_dependencies.get(dep_id)
            if dependency.get("probe") == "worker":
                configured = bool(worker_check and worker_check.get("available"))
            else:
                configured = bool(os.environ.get(env_name)) if env_name else True
            ready = configured or not required
            checks.append({
                "id": f"dependency:{dep_id}",
                "kind": str(dependency.get("kind") or "integration"),
                "label": str(dependency.get("label") or dep_id),
                "ready": ready,
                "available": configured,
                "required": required,
                "applies": applies,
                "detail": (
                    "Available"
                    if configured
                    else str(
                        (worker_check or {}).get("detail")
                        or dependency.get("missing_message")
                        or "Not configured"
                    )
                ),
            })
        if definition.transport == "http":
            token_ready = not definition.token_env or bool(os.environ.get(definition.token_env))
            worker_ready = not worker_error
            checks.append({
                "id": "transport",
                "kind": "transport",
                "label": "Worker transport",
                "ready": token_ready and worker_ready,
                "available": worker_ready,
                "detail": (
                    "Available"
                    if token_ready and worker_ready
                    else (
                        "Worker authentication is not configured"
                        if not token_ready
                        else f"Worker readiness check failed: {worker_error}"
                    )
                ),
            })
        elif definition.cwd:
            cwd_ready = Path(definition.cwd).is_dir()
            checks.append({
                "id": "transport",
                "kind": "transport",
                "label": "Worker transport",
                "ready": cwd_ready,
                "detail": "Configured" if cwd_ready else "Working directory is unavailable",
            })
        ready = definition.enabled and all(item["ready"] for item in checks)
        return {
            "status": "ready" if ready else ("disabled" if not definition.enabled else "not_ready"),
            "ready": ready,
            "checks": checks,
        }

    async def definition_view(
        self, definition: CapabilityDefinition, owner: str | None
    ) -> dict[str, Any]:
        definition = self.get_definition(definition.id)
        db = SessionLocal()
        try:
            last_run = (
                db.query(CapabilityRun)
                .filter(CapabilityRun.capability_id == definition.id)
                .filter(CapabilityRun.owner == owner if owner else CapabilityRun.owner.is_(None))
                .order_by(CapabilityRun.created_at.desc())
                .first()
            )
            active_count = (
                db.query(CapabilityRun)
                .filter(
                    CapabilityRun.capability_id == definition.id,
                    CapabilityRun.status.in_(("queued", "running")),
                )
                .filter(CapabilityRun.owner == owner if owner else CapabilityRun.owner.is_(None))
                .count()
            )
            schedules = (
                db.query(ScheduledTask)
                .filter(
                    ScheduledTask.capability_id == definition.id,
                    ScheduledTask.task_type == "capability",
                )
                .filter(ScheduledTask.owner == owner if owner else ScheduledTask.owner.is_(None))
                .order_by(ScheduledTask.next_run.asc())
                .all()
            )
            preference = (
                db.query(CapabilityPreference)
                .filter(
                    CapabilityPreference.capability_id == definition.id,
                    CapabilityPreference.owner == owner if owner else CapabilityPreference.owner.is_(None),
                )
                .first()
            )
            last_run_view = _run_to_dict(last_run) if last_run else None
            saved_defaults = (
                _json_dict(preference.values_json) if preference else {}
            )
            schedule_views = [
                {
                    "id": task.id,
                    "name": task.name,
                    "status": task.status,
                    "schedule": task.schedule,
                    "trigger_type": task.trigger_type,
                    "next_run": task.next_run.isoformat() + "Z" if task.next_run else None,
                }
                for task in schedules
            ]
        finally:
            db.close()
        default_input = {
            name: spec.default
            for name, spec in definition.inputs.items()
            if spec.default is not None
        }
        default_input.update(saved_defaults)
        view = definition.public_dict()
        view.update({
            "readiness": await self.readiness(
                definition, owner, default_input
            ),
            "active_run_count": active_count,
            "last_run": last_run_view,
            "saved_defaults": saved_defaults,
            "schedules": schedule_views,
        })
        return view

    def save_defaults(
        self, capability_id: str, raw_input: Any, owner: str | None
    ) -> dict[str, Any]:
        definition = self.registry.get(capability_id)
        values = definition.validate_input(raw_input)
        db = SessionLocal()
        try:
            query = db.query(CapabilityPreference).filter(
                CapabilityPreference.capability_id == capability_id,
                CapabilityPreference.owner == owner if owner else CapabilityPreference.owner.is_(None),
            )
            row = query.first()
            if not row:
                row = CapabilityPreference(
                    id=str(uuid.uuid4()),
                    owner=owner,
                    capability_id=capability_id,
                )
                db.add(row)
            row.values_json = dumps_input(values)
            db.commit()
            return values
        finally:
            db.close()

    def historical_definition_view(
        self, capability_id: str, owner: str | None
    ) -> dict[str, Any] | None:
        if capability_id in {item.id for item in self.registry.list()}:
            return None
        runs = self.list_runs(owner, capability_id=capability_id, limit=200)
        if not runs:
            return None
        active_count = sum(run["status"] in {"queued", "running", "cancelling"} for run in runs)
        last_run = runs[0]
        return {
            "id": capability_id,
            "name": capability_id.replace("-", " ").replace("_", " ").title(),
            "description": "Historical runs from a capability no longer registered.",
            "short_description": "Capability removed from the active registry.",
            "version": "historical",
            "implementation": "",
            "category": "Historical",
            "icon": "archive",
            "documentation_url": "",
            "enabled": False,
            "visibility": "catalog",
            "triggers": [],
            "inputs": [],
            "admin_only": False,
            "timeout_seconds": 0,
            "import_report": True,
            "model_roles": [],
            "dependencies": [],
            "output_types": ["report"],
            "report_actions": ["open", "chat", "archive", "delete", "export"],
            "progress_phases": [],
            "supports_cancel": active_count > 0,
            "supports_retry": False,
            "idempotent": False,
            "removed": True,
            "readiness": {
                "ready": False,
                "status": "removed",
                "checks": [],
            },
            "active_run_count": active_count,
            "last_run": last_run,
            "saved_defaults": {},
            "schedules": [],
        }

    def historical_definition_views(self, owner: str | None) -> list[dict[str, Any]]:
        registered = {item.id for item in self.registry.list()}
        db = SessionLocal()
        try:
            query = db.query(CapabilityRun.capability_id).distinct()
            if owner:
                query = query.filter(CapabilityRun.owner == owner)
            capability_ids = sorted(
                row[0] for row in query.all() if row[0] not in registered
            )
        finally:
            db.close()
        return [
            view
            for capability_id in capability_ids
            if (view := self.historical_definition_view(capability_id, owner))
        ]

    def get_definition(self, capability_id: str) -> CapabilityDefinition:
        from dataclasses import replace

        definition = self.registry.get(capability_id)
        db = SessionLocal()
        try:
            state = (
                db.query(CapabilityAdminState)
                .filter(CapabilityAdminState.capability_id == capability_id)
                .first()
            )
            if state is None:
                return definition
            return replace(definition, enabled=bool(state.enabled))
        finally:
            db.close()

    def set_enabled(self, capability_id: str, enabled: bool) -> CapabilityDefinition:
        self.registry.get(capability_id)
        db = SessionLocal()
        try:
            state = (
                db.query(CapabilityAdminState)
                .filter(CapabilityAdminState.capability_id == capability_id)
                .first()
            )
            if state is None:
                state = CapabilityAdminState(
                    capability_id=capability_id,
                    enabled=bool(enabled),
                )
                db.add(state)
            else:
                state.enabled = bool(enabled)
            db.commit()
        finally:
            db.close()
        return self.get_definition(capability_id)

    async def create_run(
        self,
        capability_id: str,
        raw_input: Any,
        owner: str | None,
        *,
        scheduled_task_id: str | None = None,
        task_run_id: str | None = None,
    ) -> dict[str, Any]:
        definition = self.get_definition(capability_id)
        if not definition.enabled:
            raise CapabilityConfigError("Capability is disabled")
        values = definition.validate_input(raw_input)
        readiness = await self.readiness(definition, owner, values)
        if not readiness["ready"]:
            missing = ", ".join(
                item["label"]
                for item in readiness["checks"]
                if not item["ready"]
            )
            raise CapabilityConfigError(
                f"Capability is not ready: {missing or 'configuration is incomplete'}"
            )
        run_id = str(uuid.uuid4())
        run_dir = _RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        log_path = run_dir / "output.log"
        db = SessionLocal()
        try:
            run = CapabilityRun(
                id=run_id,
                owner=owner,
                capability_id=definition.id,
                transport=definition.transport,
                status="queued",
                input_json=dumps_input(values),
                log_path=str(log_path),
                scheduled_task_id=scheduled_task_id,
                task_run_id=task_run_id,
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            result = _run_to_dict(run)
        finally:
            db.close()
        self._run_definitions[run_id] = definition
        self._spawn_driver(run_id, resume=False)
        return result

    def get_run(self, run_id: str, owner: str | None = None) -> dict[str, Any] | None:
        db = SessionLocal()
        try:
            query = db.query(CapabilityRun).filter(CapabilityRun.id == run_id)
            if owner:
                query = query.filter(CapabilityRun.owner == owner)
            run = query.first()
            return _run_to_dict(run) if run else None
        finally:
            db.close()

    def list_runs(
        self,
        owner: str | None,
        *,
        capability_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = SessionLocal()
        try:
            query = db.query(CapabilityRun)
            if owner:
                query = query.filter(CapabilityRun.owner == owner)
            if capability_id:
                query = query.filter(CapabilityRun.capability_id == capability_id)
            rows = query.order_by(CapabilityRun.created_at.desc()).limit(
                max(1, min(limit, 200))
            ).all()
            return [_run_to_dict(row) for row in rows]
        finally:
            db.close()

    def read_log(
        self, run_id: str, owner: str | None, tail: int = 400
    ) -> str | None:
        run = self.get_run(run_id, owner)
        if not run:
            return None
        db = SessionLocal()
        try:
            row = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
            path = Path(row.log_path) if row and row.log_path else None
        finally:
            db.close()
        if not path or not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max(1, min(tail, 5000)):])

    async def get_log(
        self, run_id: str, owner: str | None, tail: int = 400
    ) -> str | None:
        run = self.get_run(run_id, owner)
        if not run:
            return None
        try:
            definition = self.registry.get(run["capability_id"])
        except KeyError:
            return self.read_log(run_id, owner, tail)
        if definition.transport != "http" or not run.get("provider_run_id"):
            return self.read_log(run_id, owner, tail)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    self._url(
                        definition,
                        definition.log_path.format(
                            run_id=run["provider_run_id"]
                        ),
                    ),
                    params={"tail": max(1, min(tail, 5000))},
                    headers=self._headers(definition),
                )
                response.raise_for_status()
                payload = response.json()
                output = payload.get("output") if isinstance(payload, dict) else None
                if isinstance(output, str):
                    db = SessionLocal()
                    try:
                        row = db.query(CapabilityRun).filter(
                            CapabilityRun.id == run_id
                        ).first()
                        if row and row.log_path:
                            Path(row.log_path).write_text(
                                output, encoding="utf-8"
                            )
                    finally:
                        db.close()
                    return output
        except Exception:
            logger.warning("Capability log fetch failed for %s", run_id, exc_info=True)
        return self.read_log(run_id, owner, tail)

    async def wait(
        self,
        run_id: str,
        timeout: float | None = None,
        on_update=None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        last_snapshot = None
        while True:
            run = self.get_run(run_id)
            if not run:
                raise KeyError(run_id)
            snapshot = (
                run["status"],
                run.get("summary"),
                json.dumps(run.get("progress") or {}, sort_keys=True),
            )
            if on_update and snapshot != last_snapshot:
                callback_result = on_update(run)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
                last_snapshot = snapshot
            if run["status"] in _TERMINAL:
                return run
            if timeout is not None and time.monotonic() - started > timeout:
                raise TimeoutError(run_id)
            await asyncio.sleep(0.5)

    async def cancel(self, run_id: str, owner: str | None = None) -> bool:
        db = SessionLocal()
        try:
            query = db.query(CapabilityRun).filter(CapabilityRun.id == run_id)
            if owner:
                query = query.filter(CapabilityRun.owner == owner)
            run = query.first()
            if not run or run.status in _TERMINAL:
                return False
            definition = self._run_definitions.get(run_id)
            if definition is None:
                try:
                    definition = self.registry.get(run.capability_id)
                except KeyError:
                    definition = None
            provider_run_id = run.provider_run_id
            worker_pid = run.worker_pid
            run.status = "cancelled"
            run.error = "Cancelled by user"
            run.finished_at = _utcnow()
            db.commit()
        finally:
            db.close()

        if definition and definition.transport == "process" and worker_pid:
            await asyncio.to_thread(kill_process_tree, worker_pid)
        elif definition and definition.transport == "http" and provider_run_id:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.delete(
                        self._url(
                            definition,
                            definition.cancel_path.format(run_id=provider_run_id),
                        ),
                        headers=self._headers(definition),
                    )
            except Exception:
                logger.warning(
                    "Capability provider cancellation failed for %s", run_id,
                    exc_info=True,
                )
        task = self._tasks.get(run_id)
        if task:
            task.cancel()
        return True

    async def cancel_for_task_run(self, task_run_id: str) -> bool:
        db = SessionLocal()
        try:
            run = (
                db.query(CapabilityRun)
                .filter(
                    CapabilityRun.task_run_id == task_run_id,
                    CapabilityRun.status.in_(("queued", "running")),
                )
                .order_by(CapabilityRun.created_at.desc())
                .first()
            )
            run_id = run.id if run else None
        finally:
            db.close()
        return await self.cancel(run_id) if run_id else False

    def _spawn_driver(self, run_id: str, resume: bool) -> None:
        existing = self._tasks.get(run_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._drive(run_id, resume=resume))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._forget_driver(run_id))

    def _forget_driver(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._run_definitions.pop(run_id, None)

    async def _drive(self, run_id: str, resume: bool) -> None:
        try:
            db = SessionLocal()
            try:
                run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
                if not run or run.status in _TERMINAL:
                    return
                definition = self._run_definitions.get(run_id)
                if definition is None:
                    definition = self.registry.get(run.capability_id)
                self._run_definitions[run_id] = definition
            finally:
                db.close()
            if definition.transport == "process":
                await self._drive_process(run_id, definition, resume)
            else:
                await self._drive_http(run_id, definition, resume)
        except asyncio.CancelledError:
            raise
        except KeyError:
            await self._finish(
                run_id, "error", {}, error="Capability is no longer registered"
            )
        except Exception as exc:
            logger.exception("Capability run %s failed", run_id)
            await self._finish(
                run_id, "error", {}, error=f"{type(exc).__name__}: {exc}"
            )

    async def _drive_process(
        self, run_id: str, definition: CapabilityDefinition, resume: bool
    ) -> None:
        run_dir = _RUNS_DIR / run_id
        state_path = run_dir / "state.json"
        spec_path = run_dir / "spec.json"
        db = SessionLocal()
        try:
            run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
            if not run:
                return
            values = _json_dict(run.input_json)
            worker_pid = run.worker_pid
            if not resume or not worker_pid:
                argv = definition.build_argv(values)
                cwd = Path(definition.cwd or "")
                if not cwd.is_dir():
                    raise CapabilityConfigError(
                        f"Capability working directory does not exist: {cwd}"
                    )
                spec = {
                    "run_id": run_id,
                    "argv": argv,
                    "cwd": str(cwd),
                    "pass_env": list(definition.pass_env),
                    "timeout_seconds": definition.timeout_seconds,
                    "log_path": run.log_path,
                    "state_path": str(state_path),
                }
                atomic_write_json(str(spec_path), spec, indent=2)
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "src.capability_process_worker",
                        str(spec_path),
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **detached_popen_kwargs(),
                )
                run.worker_pid = proc.pid
                run.status = "running"
                run.started_at = _utcnow()
                db.commit()
                worker_pid = proc.pid
        finally:
            db.close()

        deadline = time.monotonic() + definition.timeout_seconds + 30
        while not self._stopping:
            current = self.get_run(run_id)
            if not current or current["status"] in _TERMINAL:
                return
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                await self._finish(
                    run_id,
                    state.get("status", "error"),
                    state.get("result") or {},
                    error=state.get("error"),
                    definition=definition,
                )
                return
            if worker_pid and not pid_alive(worker_pid):
                await self._finish(
                    run_id,
                    "interrupted",
                    {},
                    error="Capability worker exited without writing a result",
                )
                return
            if time.monotonic() > deadline:
                if worker_pid:
                    await asyncio.to_thread(kill_process_tree, worker_pid)
                await self._finish(
                    run_id,
                    "error",
                    {},
                    error="Capability worker exceeded its runtime deadline",
                )
                return
            await asyncio.sleep(_POLL_SECONDS)

    async def _drive_http(
        self, run_id: str, definition: CapabilityDefinition, resume: bool
    ) -> None:
        db = SessionLocal()
        try:
            run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
            if not run:
                return
            values = _json_dict(run.input_json)
            provider_run_id = run.provider_run_id
        finally:
            db.close()

        async with httpx.AsyncClient(timeout=30) as client:
            if not resume or not provider_run_id:
                response = await client.post(
                    self._url(definition, definition.start_path),
                    headers=self._headers(definition),
                    json={"input": values, "odysseus_run_id": run_id},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not payload.get("id"):
                    raise RuntimeError("Capability provider did not return a run id")
                provider_run_id = str(payload["id"])
                db = SessionLocal()
                try:
                    row = db.query(CapabilityRun).filter(
                        CapabilityRun.id == run_id
                    ).first()
                    if not row:
                        return
                    row.provider_run_id = provider_run_id
                    row.status = "running"
                    row.started_at = _utcnow()
                    db.commit()
                finally:
                    db.close()

            deadline = time.monotonic() + definition.timeout_seconds
            while not self._stopping:
                current = self.get_run(run_id)
                if not current or current["status"] in _TERMINAL:
                    return
                response = await client.get(
                    self._url(
                        definition,
                        definition.status_path.format(run_id=provider_run_id),
                    ),
                    headers=self._headers(definition),
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Capability provider returned a non-object status")
                provider_status = str(payload.get("status", "")).lower()
                if provider_status in {"success", "completed"}:
                    await self._finish(
                        run_id,
                        "success",
                        payload.get("result") or payload,
                        definition=definition,
                    )
                    return
                if provider_status in {"error", "failed"}:
                    await self._finish(
                        run_id,
                        "error",
                        payload.get("result") or payload,
                        error=str(payload.get("error") or "Capability provider failed"),
                        definition=definition,
                    )
                    return
                if provider_status in {"cancelled", "canceled"}:
                    await self._finish(
                        run_id,
                        "cancelled",
                        payload.get("result") or payload,
                        error="Capability provider cancelled the run",
                    )
                    return
                self._persist_progress(run_id, payload)
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Capability timed out after {definition.timeout_seconds} seconds"
                    )
                await asyncio.sleep(_POLL_SECONDS)

    def _persist_progress(self, run_id: str, payload: dict[str, Any]) -> None:
        result = payload.get("result")
        snapshot = dict(result) if isinstance(result, dict) else {}
        for key in ("progress", "metrics", "warnings", "failures", "summary", "message"):
            if key in payload and key not in snapshot:
                snapshot[key] = payload[key]
        if not snapshot:
            return
        db = SessionLocal()
        try:
            run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
            if not run or run.status in _TERMINAL:
                return
            run.result_json = json.dumps(snapshot, ensure_ascii=False)
            summary = snapshot.get("summary") or snapshot.get("message")
            if summary:
                run.summary = str(summary)[:4000]
            db.commit()
        finally:
            db.close()

    async def _finish(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any],
        *,
        error: str | None = None,
        definition: CapabilityDefinition | None = None,
    ) -> None:
        status = status if status in _TERMINAL else "error"
        if not isinstance(result, dict):
            result = {}
        db = SessionLocal()
        try:
            current = db.query(CapabilityRun).filter(
                CapabilityRun.id == run_id
            ).first()
            if not current or current.status == "cancelled":
                return
        finally:
            db.close()
        document_id = None
        report_path = None
        if status == "success" and definition and definition.import_report:
            # Capabilities return structured ``data``; Odysseus writes the
            # report from it the same way deep research does. The capability's
            # own deterministic report (if any) is the no-model fallback, so a
            # broken or unconfigured model never costs the run its output.
            await self._synthesize_report(run_id, definition, result)
            try:
                document_id, report_path = self._import_report(
                    run_id, definition, result
                )
            except Exception as exc:
                logger.warning("Capability report import failed for %s: %s", run_id, exc)
                result.setdefault("warnings", []).append(
                    f"Report import failed: {type(exc).__name__}: {exc}"
                )
        summary = str(
            result.get("summary")
            or result.get("message")
            or (
                f"{definition.name} completed"
                if definition and status == "success"
                else error
                or status
            )
        )
        persisted_result = dict(result)
        if isinstance(persisted_result.get("report"), dict):
            report_meta = dict(persisted_result["report"])
            report_meta.pop("content", None)
            if document_id:
                report_meta["document_id"] = document_id
            persisted_result["report"] = report_meta
        db = SessionLocal()
        try:
            run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
            if not run or run.status == "cancelled":
                if document_id:
                    document = db.query(Document).filter(
                        Document.id == document_id
                    ).first()
                    if document:
                        db.delete(document)
                        db.commit()
                return
            run.status = status
            run.result_json = json.dumps(persisted_result, ensure_ascii=False)
            run.summary = summary[:4000]
            run.error = error
            run.document_id = document_id
            run.report_path = report_path
            run.finished_at = _utcnow()
            db.commit()
        finally:
            db.close()

    async def _synthesize_report(
        self,
        run_id: str,
        definition: CapabilityDefinition,
        result: dict[str, Any],
    ) -> None:
        """Replace the capability's deterministic report with a synthesized one.

        Mutates ``result`` in place: on success ``result["report"]`` carries
        the model-written report and the provenance list gains the report
        model. Any failure leaves ``result`` untouched apart from a warning —
        ``_import_report`` then imports whatever report the capability itself
        provided.
        """
        from src.capability_report import synthesize_capability_report

        if not isinstance(result.get("data"), dict) or not result.get("data"):
            return
        db = SessionLocal()
        try:
            run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
            owner = run.owner if run else None
            run_input = _json_dict(run.input_json) if run else {}
        finally:
            db.close()
        try:
            report = await synthesize_capability_report(
                definition, run_id, run_input, result, owner
            )
        except Exception as exc:
            logger.warning(
                "Capability report synthesis failed for %s: %s", run_id, exc
            )
            result.setdefault("warnings", []).append(
                f"Report synthesis failed; using the capability's own report: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        if not report:
            return
        provenance = report.pop("provenance", None)
        result["report"] = report
        if provenance:
            key = "provenance" if "provenance" in result else "model_provenance"
            existing = result.get(key)
            if not isinstance(existing, list):
                existing = []
            result[key] = existing + [provenance]

    def _import_report(
        self,
        run_id: str,
        definition: CapabilityDefinition,
        result: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        content = ""
        title = str(result.get("report_title") or f"{definition.name} report")
        language = str(result.get("report_format") or "markdown")
        report_path: str | None = None
        report = result.get("report")
        if isinstance(report, dict):
            content = str(report.get("content") or "")
            title = str(report.get("title") or title)
            language = str(report.get("format") or language)
        elif isinstance(report, str):
            content = report

        raw_path = result.get("report_path")
        if raw_path and definition.transport == "process":
            base = Path(definition.cwd or "").resolve()
            candidate = Path(str(raw_path))
            if not candidate.is_absolute():
                candidate = base / candidate
            candidate = candidate.resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                raise CapabilityConfigError(
                    "Capability report_path must stay within its working directory"
                ) from None
            if not candidate.is_file():
                raise FileNotFoundError(candidate)
            if candidate.stat().st_size > _REPORT_MAX_BYTES:
                raise CapabilityConfigError(
                    f"Capability report exceeds {_REPORT_MAX_BYTES} bytes"
                )
            content = candidate.read_text(encoding="utf-8", errors="replace")
            report_path = str(candidate)
            if not result.get("report_title"):
                title = candidate.stem.replace("-", " ").replace("_", " ").title()
            if candidate.suffix.lower() in {".md", ".markdown"}:
                language = "markdown"
            elif candidate.suffix.lower() in {".txt", ".log"}:
                language = "text"

        if not content.strip():
            return None, report_path
        if len(content.encode("utf-8")) > _REPORT_MAX_BYTES:
            raise CapabilityConfigError(
                f"Capability report exceeds {_REPORT_MAX_BYTES} bytes"
            )

        db = SessionLocal()
        try:
            run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
            if not run:
                return None, report_path
            doc_id = str(uuid.uuid4())
            document = Document(
                id=doc_id,
                owner=run.owner,
                title=title[:240],
                language=language[:40],
                current_content=content,
                version_count=1,
                is_active=True,
                source_capability_id=definition.id,
                source_capability_run_id=run.id,
                source_task_id=run.scheduled_task_id,
            )
            version = DocumentVersion(
                id=str(uuid.uuid4()),
                document_id=doc_id,
                version_number=1,
                content=content,
                summary=f"Generated by capability {definition.id}",
                source="capability",
            )
            db.add(document)
            db.add(version)
            db.commit()
            return doc_id, report_path
        finally:
            db.close()

    @staticmethod
    def _url(definition: CapabilityDefinition, path: str) -> str:
        return f"{definition.base_url}{path if path.startswith('/') else '/' + path}"

    @staticmethod
    def _headers(definition: CapabilityDefinition) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if definition.token_env:
            token = os.environ.get(definition.token_env)
            if not token:
                raise CapabilityConfigError(
                    f"Missing provider token environment variable: {definition.token_env}"
                )
            headers["Authorization"] = f"Bearer {token}"
        return headers


def set_capability_manager(manager: CapabilityManager) -> None:
    global _manager
    _manager = manager


def get_capability_manager() -> CapabilityManager:
    if _manager is None:
        raise RuntimeError("Capability manager is not initialized")
    return _manager


def capability_run_to_dict(run: CapabilityRun) -> dict[str, Any]:
    return _run_to_dict(run)


async def do_manage_capabilities(
    content: str, owner: str | None = None
) -> dict[str, Any]:
    """Agent-tool adapter for capability discovery and execution."""
    from src.tool_security import owner_is_admin_or_single_user

    try:
        args = json.loads(content or "{}")
    except (TypeError, ValueError):
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    if not isinstance(args, dict):
        return {"error": "Arguments must be a JSON object", "exit_code": 1}
    manager = get_capability_manager()
    action = str(args.get("action") or "list")
    is_admin = owner_is_admin_or_single_user(owner)

    if action == "list":
        definitions = [
            manager.get_definition(item.id)
            for item in manager.registry.list()
            if not item.admin_only or is_admin
        ]
        items = await asyncio.gather(
            *(manager.definition_view(item, owner) for item in definitions)
        )
        items.extend(manager.historical_definition_views(owner))
        return {
            "response": f"Found {len(items)} registered capabilities",
            "capabilities": items,
            "exit_code": 0,
        }
    if action in {"describe", "readiness", "configure"}:
        capability_id = str(args.get("capability_id") or "")
        try:
            definition = manager.get_definition(capability_id)
        except KeyError:
            return {"error": "Capability not found", "exit_code": 1}
        if definition.admin_only and not is_admin:
            return {"error": "This capability requires an admin user", "exit_code": 1}
        if action == "configure":
            try:
                saved = manager.save_defaults(
                    capability_id, args.get("input") or {}, owner
                )
            except CapabilityConfigError as exc:
                return {"error": str(exc), "exit_code": 1}
            return {
                "response": f"Saved defaults for {definition.name}",
                "saved_defaults": saved,
                "exit_code": 0,
            }
        if action == "readiness":
            try:
                values = definition.validate_input(args.get("input") or {})
            except CapabilityConfigError as exc:
                return {"error": str(exc), "exit_code": 1}
            readiness = await manager.readiness(definition, owner, values)
            return {
                "response": readiness["status"],
                "readiness": readiness,
                "exit_code": 0,
            }
        view = await manager.definition_view(definition, owner)
        return {
            "response": view["short_description"],
            "capability": view,
            "exit_code": 0,
        }
    if action == "run":
        capability_id = str(args.get("capability_id") or "")
        try:
            definition = manager.get_definition(capability_id)
        except KeyError:
            return {"error": "Capability not found", "exit_code": 1}
        if definition.admin_only and not is_admin:
            return {"error": "This capability requires an admin user", "exit_code": 1}
        try:
            run = await manager.create_run(
                capability_id, args.get("input") or {}, owner
            )
        except CapabilityConfigError as exc:
            return {"error": str(exc), "exit_code": 1}
        return {
            "response": (
                f"Started {definition.name}. Capability run id: {run['id']}. "
                "Use status or log to monitor it."
            ),
            "run": run,
            "exit_code": 0,
        }

    run_id = str(args.get("run_id") or "")
    if not run_id:
        return {"error": "run_id is required", "exit_code": 1}
    if action == "status":
        run = manager.get_run(run_id, owner)
        if not run:
            return {"error": "Capability run not found", "exit_code": 1}
        return {
            "response": run.get("summary") or run["status"],
            "run": run,
            "exit_code": 0,
        }
    if action == "explain":
        run = manager.get_run(run_id, owner)
        if not run:
            return {"error": "Capability run not found", "exit_code": 1}
        return {
            "response": (
                run.get("summary")
                or run.get("error")
                or f"Capability run is {run['status']}"
            ),
            "run": run,
            "exit_code": 0,
        }
    if action == "log":
        output = await manager.get_log(run_id, owner, int(args.get("tail") or 200))
        if output is None:
            return {"error": "Capability run not found", "exit_code": 1}
        return {"output": output or "(no output yet)", "exit_code": 0}
    if action == "cancel":
        if not await manager.cancel(run_id, owner):
            return {"error": "Capability run is not active", "exit_code": 1}
        return {
            "response": f"Cancelled capability run {run_id}",
            "exit_code": 0,
        }
    if action == "rerun":
        previous = manager.get_run(run_id, owner)
        if not previous:
            return {"error": "Capability run not found", "exit_code": 1}
        try:
            definition = manager.get_definition(previous["capability_id"])
        except KeyError:
            return {"error": "Capability is no longer registered", "exit_code": 1}
        if definition.admin_only and not is_admin:
            return {"error": "This capability requires an admin user", "exit_code": 1}
        if not definition.supports_retry:
            return {"error": "This capability does not support reruns", "exit_code": 1}
        try:
            run = await manager.create_run(
                definition.id, previous.get("input") or {}, owner
            )
        except CapabilityConfigError as exc:
            return {"error": str(exc), "exit_code": 1}
        return {
            "response": f"Started rerun {run['id']}",
            "run": run,
            "exit_code": 0,
        }
    return {"error": f"Unknown action: {action}", "exit_code": 1}
