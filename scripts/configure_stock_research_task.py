"""Verify Stock Research, then create or update its daily task."""

from __future__ import annotations

import json
import os
import time
import urllib.request

BASE_URL = os.environ.get("ODYSSEUS_VERIFY_URL", "http://127.0.0.1:7000")
TOKEN = os.environ["ODYSSEUS_INTERNAL_TOKEN"]
OWNER = os.environ["ODYSSEUS_VERIFY_OWNER"]
TASK_NAME = "Daily Stock Research Brief"
HEADERS = {
    "X-Odysseus-Internal-Token": TOKEN,
    "X-Odysseus-Owner": OWNER,
    "Content-Type": "application/json",
}


def request_json(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path, data=data, headers=HEADERS, method=method
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    capability = request_json("/api/capabilities/stock-research")
    if not capability.get("readiness", {}).get("ready"):
        print(json.dumps({"ok": False, "stage": "readiness",
                          "readiness": capability.get("readiness")}))
        return 1

    smoke = request_json(
        "/api/capabilities/stock-research/runs",
        method="POST",
        body={"input": {"mode": "daily", "tickers": "RKLB",
                        "include_sentiment": False}},
    )
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        smoke = request_json(f"/api/capabilities/runs/{smoke['id']}")
        if smoke.get("status") not in {"queued", "running"}:
            break
        time.sleep(2)
    if smoke.get("status") != "success" or not smoke.get("document_id"):
        print(json.dumps({"ok": False, "stage": "smoke", "run": smoke}))
        return 1

    capability_input = {
        "mode": "daily",
        "tickers": "",
        "include_sentiment": True,
    }
    body = {
        "name": TASK_NAME,
        "task_type": "capability",
        "capability_id": "stock-research",
        "capability_input": capability_input,
        "trigger_type": "schedule",
        "schedule": "daily",
        "scheduled_time": os.environ.get("STOCK_RESEARCH_SCHEDULE_TIME", "07:00"),
        "notifications_enabled": True,
    }
    tasks = request_json("/api/tasks").get("tasks", [])
    task = next((item for item in tasks if item.get("name") == TASK_NAME), None)
    if task is None:
        task = request_json("/api/tasks", method="POST", body=body)
    else:
        task = request_json(f"/api/tasks/{task['id']}", method="PUT", body=body)

    print(json.dumps({
        "task_id": task["id"],
        "schedule": task.get("schedule"),
        "scheduled_time": task.get("scheduled_time"),
        "next_run": task.get("next_run"),
        "verified_capability_run_id": smoke.get("id"),
        "capability_input": task.get("capability_input"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
