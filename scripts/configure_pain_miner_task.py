"""Verify Pain Miner readiness, smoke it directly, then configure its task."""

from __future__ import annotations

import json
import os
import time
import urllib.request

BASE_URL = os.environ.get("ODYSSEUS_VERIFY_URL", "http://127.0.0.1:7000")
TOKEN = os.environ["ODYSSEUS_INTERNAL_TOKEN"]
OWNER = os.environ["ODYSSEUS_VERIFY_OWNER"]
TASK_NAME = "Weekly Profitable Pain Points"
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
    capability = request_json("/api/capabilities/pain-miner")
    if not capability.get("readiness", {}).get("ready"):
        print(json.dumps({
            "ok": False,
            "stage": "readiness",
            "readiness": capability.get("readiness"),
        }, ensure_ascii=False))
        return 1

    verification_input = {
        "since": "1d",
        "sources": "hn",
        "report": "/data/reports/pain-miner-smoke.md",
        "refresh": False,
        "max_items": 5,
    }
    verification_run = request_json(
        "/api/capabilities/pain-miner/runs",
        method="POST",
        body={"input": verification_input},
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        verification_run = request_json(
            f"/api/capabilities/runs/{verification_run['id']}"
        )
        if verification_run.get("status") not in {"queued", "running"}:
            break
        time.sleep(1)
    if verification_run.get("status") != "success":
        print(json.dumps({
            "ok": False,
            "stage": "smoke",
            "run": verification_run,
        }, ensure_ascii=False))
        return 1

    production_input = {
        "since": "7d",
        "sources": (
            "hn,reddit,austender,austender_ocds,g2,github,"
            "appstore,stackexchange,bluesky,rss"
        ),
        "report": "/data/reports/pain-miner-weekly.md",
        "refresh": True,
    }
    create_body = {
        "name": TASK_NAME,
        "task_type": "capability",
        "capability_id": "pain-miner",
        "capability_input": production_input,
        "trigger_type": "schedule",
        "schedule": "weekly",
        "scheduled_day": 6,
        "scheduled_time": "09:00",
        "notifications_enabled": True,
    }
    tasks = request_json("/api/tasks").get("tasks", [])
    task = next((item for item in tasks if item.get("name") == TASK_NAME), None)
    if task is None:
        task = request_json("/api/tasks", method="POST", body=create_body)
    else:
        task = request_json(
            f"/api/tasks/{task['id']}",
            method="PUT",
            body=create_body,
        )

    print(
        json.dumps(
            {
                "task_id": task["id"],
                "schedule": task.get("schedule"),
                "scheduled_day": task.get("scheduled_day"),
                "scheduled_time": task.get("scheduled_time"),
                "next_run": task.get("next_run"),
                "verified_capability_run_id": verification_run.get("id"),
                "verified_run_status": verification_run.get("status"),
                "capability_input": task.get("capability_input"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
