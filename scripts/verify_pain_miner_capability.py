"""Authenticated deterministic smoke test for the Pain Miner capability."""

from __future__ import annotations

import json
import os
import time
import urllib.request

BASE_URL = os.environ.get("ODYSSEUS_VERIFY_URL", "http://127.0.0.1:7000")
TOKEN = os.environ["ODYSSEUS_INTERNAL_TOKEN"]
HEADERS = {
    "X-Odysseus-Internal-Token": TOKEN,
    "Content-Type": "application/json",
}
if owner := os.environ.get("ODYSSEUS_VERIFY_OWNER"):
    HEADERS["X-Odysseus-Owner"] = owner
TERMINAL = {"success", "error", "cancelled", "interrupted"}


def request_json(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path, data=data, headers=HEADERS, method=method
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    request_json("/api/capabilities/reload", method="POST")
    listing = request_json("/api/capabilities")
    ids = [item["id"] for item in listing.get("capabilities", [])]
    if "pain-miner" not in ids:
        raise RuntimeError("pain-miner is not registered")

    refresh = os.environ.get("ODYSSEUS_VERIFY_REFRESH", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    created = request_json(
        "/api/capabilities/pain-miner/runs",
        method="POST",
        body={
            "input": {
                "since": os.environ.get("ODYSSEUS_VERIFY_SINCE", "1d"),
                "sources": os.environ.get("ODYSSEUS_VERIFY_SOURCE", "g2"),
                "report": os.environ.get(
                    "ODYSSEUS_VERIFY_REPORT",
                    "/data/reports/integration-no-refresh.md",
                ),
                "refresh": refresh,
                "max_items": 5,
            }
        },
    )
    run_id = created["id"]
    deadline = time.monotonic() + 120
    current = created
    while current.get("status") not in TERMINAL:
        if time.monotonic() >= deadline:
            raise TimeoutError(run_id)
        time.sleep(1)
        current = request_json(f"/api/capabilities/runs/{run_id}")

    output = {
        "id": run_id,
        "status": current.get("status"),
        "summary": current.get("summary"),
        "document_id": current.get("document_id"),
        "report_path": current.get("report_path"),
        "result": current.get("result"),
        "error": current.get("error"),
    }
    print(json.dumps(output, ensure_ascii=False))
    if current.get("status") != "success" or not current.get("document_id"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
