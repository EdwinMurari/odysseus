"""Contract tests for the v1 run-lifecycle protocol.

These assertions ARE the contract Odysseus's capability_runner consumes;
if one needs changing, capability_runner must change with it.
"""

import time

from fastapi import HTTPException
from fastapi.testclient import TestClient

from capability_kit.runs import FileRunStore, MemoryRunStore
from capability_kit.worker import create_worker_app, env_readiness

TOKEN = "worker-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class InstantExecutor:
    """Synchronous executor double: completes runs inline via the store."""

    def __init__(self, store, *, fail=False):
        self.store = store
        self.fail = fail
        self.cancelled: list[str] = []
        self.logs: dict[str, str] = {}

    def validate(self, raw_input):
        if raw_input.get("mode") == "invalid":
            raise HTTPException(422, "invalid mode")

    def start(self, run_id, raw_input, odysseus_run_id):
        self.logs[run_id] = "line1\nline2\nline3"
        if self.fail:
            self.store.transition(
                run_id, {"queued", "running"},
                status="error", error="boom", finished_at=time.time(),
            )
        else:
            self.store.transition(
                run_id, {"queued", "running"},
                status="success",
                result={"summary": "ok", "odysseus_run_id": odysseus_run_id},
                finished_at=time.time(),
            )

    def cancel(self, run_id):
        self.cancelled.append(run_id)

    def read_log(self, run_id, tail):
        return "\n".join(self.logs.get(run_id, "").splitlines()[-tail:])


class NeverFinishesExecutor(InstantExecutor):
    def start(self, run_id, raw_input, odysseus_run_id):
        self.store.transition(run_id, {"queued"}, status="running")


def _app(executor_cls=InstantExecutor, store=None, token=TOKEN, **kw):
    store = store if store is not None else MemoryRunStore()
    executor = executor_cls(store, **kw)
    app = create_worker_app(
        title="test worker",
        store=store,
        executor=executor,
        token_provider=lambda: token,
        readiness_provider=lambda: [
            {"id": "dep", "available": True, "detail": "Available"}
        ],
    )
    return TestClient(app), executor, store


def test_missing_token_is_503_not_open_door():
    client, _, _ = _app(token="")
    assert client.post("/v1/runs", json={"input": {}}, headers=AUTH).status_code == 503


def test_wrong_or_absent_bearer_is_401():
    client, _, _ = _app()
    assert client.post("/v1/runs", json={"input": {}}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/v1/runs", json={"input": {}}, headers=bad).status_code == 401


def test_create_run_returns_id_and_queued():
    client, _, _ = _app()
    body = client.post("/v1/runs", json={"input": {}}, headers=AUTH).json()
    assert body["id"] and body["status"] == "queued"


def test_status_view_carries_result_and_error_fields():
    client, _, _ = _app()
    run_id = client.post(
        "/v1/runs", json={"input": {}, "odysseus_run_id": "odys-1"}, headers=AUTH
    ).json()["id"]
    view = client.get(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert view["status"] == "success"
    assert view["result"]["odysseus_run_id"] == "odys-1"
    assert view["error"] is None


def test_executor_validation_rejects_before_run_creation():
    client, _, store = _app()
    response = client.post(
        "/v1/runs", json={"input": {"mode": "invalid"}}, headers=AUTH
    )
    assert response.status_code == 422


def test_failed_run_reports_error():
    client, _, _ = _app(fail=True)
    run_id = client.post("/v1/runs", json={"input": {}}, headers=AUTH).json()["id"]
    view = client.get(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert view["status"] == "error" and view["error"] == "boom"


def test_log_endpoint_uses_output_key_and_tail():
    """Odysseus reads payload["output"] — the key stock-research used to get wrong."""
    client, _, _ = _app()
    run_id = client.post("/v1/runs", json={"input": {}}, headers=AUTH).json()["id"]
    body = client.get(f"/v1/runs/{run_id}/log", params={"tail": 2}, headers=AUTH).json()
    assert body == {"id": run_id, "output": "line2\nline3"}


def test_cancel_active_run_marks_cancelled_and_notifies_executor():
    client, executor, _ = _app(NeverFinishesExecutor)
    run_id = client.post("/v1/runs", json={"input": {}}, headers=AUTH).json()["id"]
    body = client.delete(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert body["status"] == "cancelled"
    assert executor.cancelled == [run_id]
    assert client.get(f"/v1/runs/{run_id}", headers=AUTH).json()["status"] == "cancelled"


def test_cancel_terminal_run_is_idempotent():
    client, executor, _ = _app()
    run_id = client.post("/v1/runs", json={"input": {}}, headers=AUTH).json()["id"]
    body = client.delete(f"/v1/runs/{run_id}", headers=AUTH).json()
    assert body["status"] == "success"
    assert executor.cancelled == []


def test_late_finish_cannot_overwrite_cancellation():
    """The race both old workers handled ad hoc: finish after cancel is a no-op."""
    client, executor, store = _app(NeverFinishesExecutor)
    run_id = client.post("/v1/runs", json={"input": {}}, headers=AUTH).json()["id"]
    client.delete(f"/v1/runs/{run_id}", headers=AUTH)
    assert store.transition(
        run_id, {"queued", "running"}, status="success", result={"late": True}
    ) is None
    assert client.get(f"/v1/runs/{run_id}", headers=AUTH).json()["status"] == "cancelled"


def test_unknown_run_is_404():
    client, _, _ = _app()
    assert client.get("/v1/runs/nope", headers=AUTH).status_code == 404
    assert client.delete("/v1/runs/nope", headers=AUTH).status_code == 404


def test_readiness_reports_ready_and_dependencies():
    client, _, _ = _app()
    assert client.get("/v1/readiness", headers=AUTH).json() == {
        "ready": True,
        "dependencies": [{"id": "dep", "available": True, "detail": "Available"}],
    }


def test_healthz_is_open():
    client, _, _ = _app()
    assert client.get("/healthz").json() == {"ok": True}


def test_env_readiness_never_returns_secret_values(monkeypatch):
    monkeypatch.setenv("SOURCE_API_TOKEN", "super-secret")
    report = env_readiness({
        "source-api": {"env": ["SOURCE_API_TOKEN"], "missing_message": "unavailable"},
        "other": {"env": ["NOT_SET_VAR"], "missing_message": "other unavailable"},
    })
    assert report == [
        {"id": "source-api", "available": True, "detail": "Available"},
        {"id": "other", "available": False, "detail": "other unavailable"},
    ]
    assert "super-secret" not in str(report)


def test_file_store_recovers_interrupted_runs(tmp_path):
    store = FileRunStore(tmp_path)
    store.create({"id": "a", "status": "running"})
    store.create({"id": "b", "status": "success"})
    assert store.recover_interrupted("Worker restarted") == 1
    assert store.get("a")["status"] == "error"
    assert store.get("b")["status"] == "success"


def test_memory_store_clear_supports_test_isolation():
    store = MemoryRunStore()
    store.create({"id": "x", "status": "queued"})
    store.clear()
    assert store.get("x") is None
