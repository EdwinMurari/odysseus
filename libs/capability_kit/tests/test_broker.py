import json

import httpx
import pytest
from pydantic import BaseModel

from capability_kit.broker import BrokerClient, BrokerError


class DummyModel(BaseModel):
    value: str


def _client(handler) -> BrokerClient:
    return BrokerClient(
        "http://odysseus:7000", "broker-token", "run-1", "painminer.tag",
        transport=httpx.MockTransport(handler),
    )


def test_structured_call_posts_contract_and_returns_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/capability-models/structured"
        assert request.headers["Authorization"] == "Bearer broker-token"
        payload = json.loads(request.content)
        assert payload == {
            "run_id": "run-1",
            "role": "painminer.tag",
            "prompt": "prompt",
            "schema": {"type": "object"},
        }
        return httpx.Response(200, json={"data": {"value": "pain"}})

    assert _client(handler).structured_call("prompt", {"type": "object"}) == {
        "value": "pain"
    }


def test_provenance_is_filtered_normalized_and_deduplicated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {"value": "x"},
            "provenance": {
                "role": "painminer.tag",
                "model": "model-1",
                "endpoint": "http://model/v1",
                "internal_detail": "must not leak",
            },
        })

    client = _client(handler)
    client.structured_call("p", {})
    client.structured_call("p", {})  # identical provenance — not duplicated

    assert client.provenance() == [{
        "role": "painminer.tag", "model": "model-1", "endpoint": "http://model/v1",
    }]


def test_broker_failure_surfaces_detail():
    client = _client(lambda r: httpx.Response(503, json={"detail": "model unavailable"}))
    with pytest.raises(BrokerError, match="model unavailable"):
        client.structured_call("p", {})


def test_broker_error_is_a_runtime_error_for_existing_callers():
    assert issubclass(BrokerError, RuntimeError)


def test_invalid_payload_rejected():
    client = _client(lambda r: httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(BrokerError, match="invalid response"):
        client.structured_call("p", {})


def test_structured_call_model_round_trips_pydantic():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["schema"]["required"] == ["value"]
        return httpx.Response(200, json={"data": {"value": "typed"}})

    result = _client(handler).structured_call_model("p", DummyModel)
    assert result == DummyModel(value="typed")


def test_from_environment_requires_full_wiring(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_MODEL_BROKER_URL", raising=False)
    monkeypatch.delenv("ODYSSEUS_CAPABILITY_MODEL_TOKEN", raising=False)
    assert BrokerClient.from_environment("run-1", "painminer.tag") is None

    monkeypatch.setenv("ODYSSEUS_MODEL_BROKER_URL", "http://odysseus:7000")
    monkeypatch.setenv("ODYSSEUS_CAPABILITY_MODEL_TOKEN", "tok")
    client = BrokerClient.from_environment("run-1", "painminer.tag")
    assert client is not None and client.role == "painminer.tag"


def test_user_agent_header_sent_when_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "painminer/0.1 (Edwin e@example.com)"
        return httpx.Response(200, json={"data": {}})

    client = BrokerClient(
        "http://odysseus:7000", "tok", "run-1", "painminer.tag",
        transport=httpx.MockTransport(handler),
        user_agent="painminer/0.1 (Edwin e@example.com)",
    )
    client.structured_call("p", {})
