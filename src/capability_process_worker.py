"""Detached argv worker used by the process capability transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json
from core.platform_compat import kill_process_tree


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


def run(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    log_path = Path(spec["log_path"])
    state_path = Path(spec["state_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    base_env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {
            "PATH",
            "HOME",
            "USER",
            "USERNAME",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        }
    }
    for name in spec.get("pass_env", []):
        if name in os.environ:
            base_env[name] = os.environ[name]
    base_env["ODYSSEUS_CAPABILITY_RUN_ID"] = spec["run_id"]

    started = datetime.now(timezone.utc).isoformat()
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            proc = subprocess.Popen(
                spec["argv"],
                cwd=spec["cwd"],
                env=base_env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            try:
                return_code = proc.wait(timeout=spec["timeout_seconds"])
            except subprocess.TimeoutExpired:
                kill_process_tree(proc.pid)
                raise
        output = log_path.read_text(encoding="utf-8", errors="replace")
        result = _parse_result(output)
        state = {
            "status": "success" if return_code == 0 else "error",
            "exit_code": return_code,
            "result": result,
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        if return_code != 0 and not result.get("error"):
            state["error"] = f"Capability exited with code {return_code}"
    except subprocess.TimeoutExpired:
        output = (
            log_path.read_text(encoding="utf-8", errors="replace")
            if log_path.exists()
            else ""
        )
        state = {
            "status": "error",
            "exit_code": -1,
            "error": f"Capability timed out after {spec['timeout_seconds']} seconds",
            "result": _parse_result(output),
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        state = {
            "status": "error",
            "exit_code": -1,
            "error": f"{type(exc).__name__}: {exc}",
            "result": {},
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        log_path.write_text(state["error"] + "\n", encoding="utf-8")
    atomic_write_json(str(state_path), state, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
