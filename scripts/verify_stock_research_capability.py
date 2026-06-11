"""Authenticated smoke test for the Stock Research capability."""

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
    capability = request_json("/api/capabilities/stock-research")
    if capability.get("id") != "stock-research":
        raise RuntimeError("stock-research is not registered")
    if not capability.get("readiness", {}).get("ready"):
        print(json.dumps({"ok": False, "stage": "readiness",
                          "readiness": capability.get("readiness")}))
        return 1

    created = request_json(
        "/api/capabilities/stock-research/runs",
        method="POST",
        body={"input": {
            "mode": os.environ.get("STOCK_RESEARCH_VERIFY_MODE", "daily"),
            "tickers": os.environ.get("STOCK_RESEARCH_VERIFY_TICKERS", "RKLB"),
            "include_sentiment": os.environ.get(
                "STOCK_RESEARCH_VERIFY_SENTIMENT", "false"
            ).lower() in {"1", "true", "yes"},
        }},
    )
    run_id = created["id"]
    deadline = time.monotonic() + int(
        os.environ.get("STOCK_RESEARCH_VERIFY_TIMEOUT", "1800")
    )
    current = created
    while current.get("status") not in TERMINAL:
        if time.monotonic() >= deadline:
            raise TimeoutError(run_id)
        time.sleep(2)
        current = request_json(f"/api/capabilities/runs/{run_id}")

    output = {
        "id": run_id,
        "status": current.get("status"),
        "summary": current.get("summary"),
        "document_id": current.get("document_id"),
        "warnings": current.get("warnings"),
        "provenance": current.get("provenance"),
        "error": current.get("error"),
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0 if current.get("status") == "success" and current.get("document_id") else 1


if __name__ == "__main__":
    raise SystemExit(main())
