"""Generic Docker capability worker implementing the Odysseus v1 protocol.

The worker runs one fixed argv command configured by environment variables.
Inputs are converted to argv using a typed JSON specification. No shell is
used, and clients cannot replace the executable or working directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Odysseus Capability Worker", version="1")

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

_tasks: dict[str, asyncio.Task] = {}
_processes: dict[str, asyncio.subprocess.Process] = {}


class RunCreate(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    odysseus_run_id: str | None = None


def _auth(authorization: str | None) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "Invalid capability token")


def _path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def _load(run_id: str) -> dict[str, Any] | None:
    path = _path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save(run: dict[str, Any]) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(run["id"])
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


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


async def _execute(run_id: str, argv: list[str]) -> None:
    run = _load(run_id)
    if not run:
        return
    run["status"] = "running"
    run["started_at"] = time.time()
    _save(run)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
    }
    for name in PASS_ENV:
        if name in os.environ:
            env[name] = os.environ[name]
    env["ODYSSEUS_CAPABILITY_RUN_ID"] = run_id
    if run.get("odysseus_run_id"):
        env["ODYSSEUS_RUN_ID"] = str(run["odysseus_run_id"])
    log_path = RUNS_DIR / f"{run_id}.log"
    reader: asyncio.Task | None = None
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
        _processes[run_id] = proc
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
        run = _load(run_id) or run
        if run.get("status") == "cancelled":
            return
        run["status"] = "success" if proc.returncode == 0 else "error"
        run["result"] = result
        if proc.returncode != 0:
            run["error"] = result.get("error") or f"Exited with code {proc.returncode}"
    except asyncio.TimeoutError:
        proc = _processes.get(run_id)
        if proc and proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
        run = _load(run_id) or run
        run["status"] = "error"
        run["error"] = f"Timed out after {TIMEOUT} seconds"
    except Exception as exc:
        run = _load(run_id) or run
        run["status"] = "error"
        run["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if reader and not reader.done():
            try:
                await asyncio.wait_for(reader, timeout=5)
            except Exception:
                reader.cancel()
        _processes.pop(run_id, None)
        _tasks.pop(run_id, None)
        run["finished_at"] = time.time()
        _save(run)


@app.on_event("startup")
async def startup() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for path in RUNS_DIR.glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if run.get("status") in {"queued", "running"}:
            run["status"] = "error"
            run["error"] = "Worker restarted while the capability was running"
            run["finished_at"] = time.time()
            _save(run)


@app.post("/v1/runs")
async def create_run(
    body: RunCreate, authorization: str | None = Header(default=None)
):
    _auth(authorization)
    argv = _argv(body.input)
    run_id = uuid.uuid4().hex
    run = {
        "id": run_id,
        "status": "queued",
        "input": body.input,
        "odysseus_run_id": body.odysseus_run_id,
        "created_at": time.time(),
        "result": {},
    }
    _save(run)
    _tasks[run_id] = asyncio.create_task(_execute(run_id, argv))
    return {"id": run_id, "status": "queued"}


@app.get("/v1/readiness")
async def readiness(authorization: str | None = Header(default=None)):
    _auth(authorization)
    dependencies = []
    for dependency_id, spec in READINESS.items():
        if not isinstance(spec, dict):
            continue
        env_names = spec.get("env") or []
        if isinstance(env_names, str):
            env_names = [env_names]
        available = bool(env_names) and all(
            bool(os.environ.get(str(name))) for name in env_names
        )
        dependencies.append({
            "id": str(dependency_id),
            "available": available,
            "detail": (
                str(spec.get("ready_message") or "Available")
                if available
                else str(spec.get("missing_message") or "Not configured")
            ),
        })
    return {"ready": all(item["available"] for item in dependencies), "dependencies": dependencies}


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    run = _load(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/v1/runs/{run_id}/log")
async def get_run_log(
    run_id: str,
    tail: int = 400,
    authorization: str | None = Header(default=None),
):
    _auth(authorization)
    if not _load(run_id):
        raise HTTPException(404, "Run not found")
    log_path = RUNS_DIR / f"{run_id}.log"
    if not log_path.exists():
        return {"id": run_id, "output": ""}
    lines = log_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    return {
        "id": run_id,
        "output": "\n".join(lines[-max(1, min(tail, 5000)):]),
    }


@app.delete("/v1/runs/{run_id}")
async def cancel_run(run_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    run = _load(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.get("status") in {"success", "error", "cancelled"}:
        raise HTTPException(409, "Run is not active")
    run["status"] = "cancelled"
    run["error"] = "Cancelled by Odysseus"
    run["finished_at"] = time.time()
    _save(run)
    proc = _processes.get(run_id)
    if proc and proc.returncode is None:
        os.killpg(proc.pid, signal.SIGTERM)
    task = _tasks.get(run_id)
    if task:
        task.cancel()
    return {"ok": True}
