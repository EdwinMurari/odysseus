"""Generic Docker capability worker implementing the Odysseus v1 protocol.

The worker runs one fixed argv command configured by environment variables.
Inputs are converted to argv using a typed JSON specification. No shell is
used, and clients cannot replace the executable or working directory.

The HTTP protocol surface (auth, run lifecycle, log/readiness endpoints,
cancel/finish race) comes from ``capability_kit.worker``; this module owns
only what is specific to subprocess execution: argv construction, env
passing, log streaming, result parsing, and report inlining.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from capability_kit.runs import ACTIVE_STATUSES, FileRunStore
from capability_kit.worker import (
    create_worker_app,
    env_readiness,
    readiness_report as _kit_readiness_report,
    tail_lines,
)

RUNS_DIR = Path(os.environ.get("CAPABILITY_RUNS_DIR", "/data/runs"))
COMMAND = json.loads(os.environ.get("CAPABILITY_COMMAND_JSON", "[]"))
INPUTS = json.loads(os.environ.get("CAPABILITY_INPUTS_JSON", "{}"))
WORKDIR = os.environ.get("CAPABILITY_WORKDIR", "/capability")
TOKEN = os.environ.get("CAPABILITY_TOKEN", "")
TIMEOUT = int(os.environ.get("CAPABILITY_TIMEOUT_SECONDS", "1800"))
PASS_ENV = json.loads(os.environ.get("CAPABILITY_PASS_ENV_JSON", "[]"))
READINESS = json.loads(os.environ.get("CAPABILITY_READINESS_JSON", "{}"))
REPORT_ROOTS = [
    Path(value).resolve()
    for value in json.loads(
        os.environ.get(
            "CAPABILITY_REPORT_ROOTS_JSON",
            json.dumps([WORKDIR, "/data/reports"]),
        )
    )
]


def _coerce(name: str, value: Any, type_name: str) -> Any:
    try:
        if type_name in {"string", "text"}:
            if isinstance(value, (dict, list)):
                raise TypeError
            return str(value)
        if type_name == "array":
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                return [item.strip() for item in value.split(",") if item.strip()]
            raise TypeError
        if type_name == "integer":
            if isinstance(value, bool):
                raise TypeError
            return int(value)
        if type_name == "number":
            if isinstance(value, bool):
                raise TypeError
            return float(value)
        if type_name == "boolean":
            if isinstance(value, bool):
                return value
            raise TypeError
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a {type_name}") from None
    raise HTTPException(500, f"Worker has invalid type for {name}")


def _argv(raw: dict[str, Any]) -> list[str]:
    if not isinstance(COMMAND, list) or not COMMAND or not all(
        isinstance(part, str) and part for part in COMMAND
    ):
        raise HTTPException(500, "CAPABILITY_COMMAND_JSON is invalid")
    if not isinstance(INPUTS, dict):
        raise HTTPException(500, "CAPABILITY_INPUTS_JSON is invalid")
    unknown = sorted(set(raw) - set(INPUTS))
    if unknown:
        raise HTTPException(400, f"Unknown input field(s): {', '.join(unknown)}")
    argv = list(COMMAND)
    for name, spec in INPUTS.items():
        if not isinstance(spec, dict):
            raise HTTPException(500, f"Invalid worker input specification: {name}")
        value = raw.get(name, spec.get("default"))
        if value is None:
            if spec.get("required"):
                raise HTTPException(400, f"Missing required input: {name}")
            continue
        type_name = str(spec.get("type", "string"))
        value = _coerce(name, value, type_name)
        choices = spec.get("choices") or []
        if choices and value not in choices:
            raise HTTPException(400, f"{name} is not an allowed value")
        flag = spec.get("flag")
        if type_name == "boolean":
            if value and flag:
                argv.append(str(flag))
            elif not value and spec.get("false_flag"):
                argv.append(str(spec["false_flag"]))
        elif flag:
            argv.extend([
                str(flag),
                ",".join(str(item) for item in value)
                if type_name == "array"
                else str(value),
            ])
        else:
            argv.append(
                ",".join(str(item) for item in value)
                if type_name == "array"
                else str(value)
            )
    return argv


def _parse_result(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        text = line.strip()
        if text.startswith("ODYSSEUS_RESULT="):
            text = text.split("=", 1)[1].strip()
        if not text.startswith("{"):
            continue
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _inline_report(result: dict[str, Any]) -> dict[str, Any]:
    """Inline a local report so the isolated worker can return it to Odysseus."""
    if result.get("report") or not result.get("report_path"):
        return result
    candidate = Path(str(result["report_path"]))
    if not candidate.is_absolute():
        candidate = Path(WORKDIR) / candidate
    candidate = candidate.resolve()
    if not any(
        candidate == root or root in candidate.parents
        for root in REPORT_ROOTS
    ):
        result.setdefault("warnings", []).append(
            "report_path was outside CAPABILITY_REPORT_ROOTS_JSON"
        )
        return result
    if not candidate.is_file():
        result.setdefault("warnings", []).append("report_path does not exist")
        return result
    suffix = candidate.suffix.lower()
    report_format = (
        "markdown" if suffix in {".md", ".markdown"}
        else "text"
    )
    result["report"] = {
        "title": str(result.get("report_title") or candidate.stem.replace("-", " ").title()),
        "format": str(result.get("report_format") or report_format),
        "content": candidate.read_text(encoding="utf-8", errors="replace"),
    }
    return result


def readiness_report() -> dict[str, Any]:
    """Dependency availability from CAPABILITY_READINESS_JSON (no secret values)."""
    return _kit_readiness_report(env_readiness(READINESS))


class SubprocessExecutor:
    """Runs the configured argv command per run on the app's event loop."""

    def __init__(self, store: FileRunStore) -> None:
        self.store = store
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- RunExecutor protocol -------------------------------------------------

    def validate(self, raw_input: dict[str, Any]) -> None:
        _argv(raw_input)

    def start(
        self, run_id: str, raw_input: dict[str, Any], odysseus_run_id: str | None
    ) -> None:
        if self._loop is None:
            raise HTTPException(503, "worker event loop is not ready")
        argv = _argv(raw_input)
        future = asyncio.run_coroutine_threadsafe(
            self._execute(run_id, argv, odysseus_run_id), self._loop
        )
        # keep a handle so cancel() can reach the task
        self._tasks[run_id] = future  # type: ignore[assignment]

    def cancel(self, run_id: str) -> None:
        proc = self._processes.get(run_id)
        if proc and proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
        task = self._tasks.get(run_id)
        if task:
            task.cancel()

    def read_log(self, run_id: str, tail: int) -> str:
        log_path = RUNS_DIR / f"{run_id}.log"
        if not log_path.exists():
            return ""
        return tail_lines(
            log_path.read_text(encoding="utf-8", errors="replace"), tail
        )

    # -- subprocess execution -------------------------------------------------

    async def _execute(
        self, run_id: str, argv: list[str], odysseus_run_id: str | None
    ) -> None:
        if self.store.transition(
            run_id, {"queued"}, status="running", started_at=time.time()
        ) is None:
            return  # cancelled while queued
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
        }
        for name in PASS_ENV:
            if name in os.environ:
                env[name] = os.environ[name]
        env["ODYSSEUS_CAPABILITY_RUN_ID"] = run_id
        if odysseus_run_id:
            env["ODYSSEUS_RUN_ID"] = str(odysseus_run_id)
        log_path = RUNS_DIR / f"{run_id}.log"
        reader: asyncio.Task | None = None
        finish: dict[str, Any]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=WORKDIR,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            self._processes[run_id] = proc

            async def stream_output() -> None:
                with log_path.open("wb") as log_file:
                    while True:
                        chunk = await proc.stdout.read(65536)
                        if not chunk:
                            break
                        log_file.write(chunk)
                        log_file.flush()

            reader = asyncio.create_task(stream_output())
            await asyncio.wait_for(proc.wait(), timeout=TIMEOUT)
            await reader
            output = log_path.read_text(encoding="utf-8", errors="replace")
            result = _inline_report(_parse_result(output))
            if proc.returncode == 0:
                finish = {"status": "success", "result": result}
            else:
                finish = {
                    "status": "error",
                    "result": result,
                    "error": result.get("error")
                    or f"Exited with code {proc.returncode}",
                }
        except asyncio.TimeoutError:
            proc = self._processes.get(run_id)
            if proc and proc.returncode is None:
                os.killpg(proc.pid, signal.SIGTERM)
            finish = {
                "status": "error",
                "error": f"Timed out after {TIMEOUT} seconds",
            }
        except Exception as exc:
            finish = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if reader and not reader.done():
                try:
                    await asyncio.wait_for(reader, timeout=5)
                except Exception:
                    reader.cancel()
            self._processes.pop(run_id, None)
            self._tasks.pop(run_id, None)
        # transition() guards the cancel race: a cancelled record stays cancelled.
        self.store.transition(
            run_id, ACTIVE_STATUSES, finished_at=time.time(), **finish
        )


_store = FileRunStore(RUNS_DIR)
_executor = SubprocessExecutor(_store)

app = create_worker_app(
    title="Odysseus Capability Worker",
    store=_store,
    executor=_executor,
    token_provider=lambda: TOKEN,
    readiness_provider=lambda: env_readiness(READINESS),
)


@app.on_event("startup")
async def startup() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _executor.bind_loop(asyncio.get_running_loop())
    _store.recover_interrupted("Worker restarted while the capability was running")
