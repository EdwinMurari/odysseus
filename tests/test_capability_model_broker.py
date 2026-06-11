import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, CapabilityRun
from src.capabilities import CapabilityConfigError, CapabilityRegistry
from src.capability_model_broker import (
    CapabilityModelAuthorizationError,
    CapabilityModelBrokerError,
    CapabilityModelRequestError,
    authorize_broker_call,
    invoke_structured_model,
)


def _write_registry(path, *, enabled=True, max_schema_depth=20):
    path.write_text(
        f"""
version: 1
capabilities:
  - id: pain-miner
    name: Pain Miner
    transport:
      type: http
      base_url: http://pain-miner:8080
model_roles:
  painminer.tag:
    setting_prefix: utility
    capabilities: [pain-miner]
    enabled: {str(enabled).lower()}
    max_schema_depth: {max_schema_depth}
""",
        encoding="utf-8",
    )


def _session(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'broker.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    import src.capability_model_broker as broker

    monkeypatch.setattr(broker, "SessionLocal", factory)
    return factory


def _add_run(factory, *, status="running", capability_id="pain-miner"):
    db = factory()
    try:
        db.add(
            CapabilityRun(
                id="run-1",
                owner="alice",
                capability_id=capability_id,
                transport="http",
                status=status,
                input_json="{}",
            )
        )
        db.commit()
    finally:
        db.close()


def test_registry_parses_model_roles_and_rejects_unknown_capabilities(tmp_path):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config)

    role = CapabilityRegistry(str(config)).get_model_role("painminer.tag")

    assert role.setting_prefix == "utility"
    assert role.capabilities == ("pain-miner",)

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "capabilities: [pain-miner]", "capabilities: [missing]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(CapabilityConfigError, match="registered capabilities"):
        CapabilityRegistry(str(config))


def test_model_role_authorization_requires_active_allowlisted_run(tmp_path, monkeypatch):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config)
    registry = CapabilityRegistry(str(config))
    factory = _session(tmp_path, monkeypatch)
    _add_run(factory)

    context = authorize_broker_call(registry, "run-1", "painminer.tag")

    assert context.owner == "alice"
    assert context.capability_id == "pain-miner"

    db = factory()
    try:
        db.query(CapabilityRun).filter(CapabilityRun.id == "run-1").update(
            {"status": "success"}
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(CapabilityModelAuthorizationError, match="not active"):
        authorize_broker_call(registry, "run-1", "painminer.tag")


def test_disabled_and_unknown_model_roles_fail_closed(tmp_path, monkeypatch):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config, enabled=False)
    registry = CapabilityRegistry(str(config))
    factory = _session(tmp_path, monkeypatch)
    _add_run(factory)

    with pytest.raises(CapabilityModelAuthorizationError, match="disabled"):
        authorize_broker_call(registry, "run-1", "painminer.tag")
    with pytest.raises(CapabilityModelAuthorizationError, match="Unknown"):
        authorize_broker_call(registry, "run-1", "painminer.cluster")


@pytest.mark.asyncio
async def test_structured_model_call_resolves_owner_and_returns_provenance(
    tmp_path, monkeypatch
):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config)
    registry = CapabilityRegistry(str(config))
    factory = _session(tmp_path, monkeypatch)
    _add_run(factory)
    context = authorize_broker_call(registry, "run-1", "painminer.tag")

    import src.capability_model_broker as broker

    resolved = {}

    def fake_resolve(prefix, owner=None):
        resolved.update(prefix=prefix, owner=owner)
        return "http://model/v1/chat/completions", "model-1", {"Authorization": "secret"}

    async def fake_call(url, model, messages, **kwargs):
        assert kwargs["headers"] == {"Authorization": "secret"}
        assert json.loads(messages[0]["content"].split("JSON Schema:\n", 1)[1])
        return '{"answer":"profitable pain"}'

    monkeypatch.setattr(broker, "resolve_endpoint", fake_resolve)
    monkeypatch.setattr(broker, "llm_call_async", fake_call)

    result = await invoke_structured_model(
        context,
        "Find the pain point",
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    assert resolved == {"prefix": "utility", "owner": "alice"}
    assert result["data"] == {"answer": "profitable pain"}
    assert result["provenance"] == {
        "role": "painminer.tag",
        "setting_prefix": "utility",
        "model": "model-1",
        "endpoint": "http://model/v1/chat/completions",
    }


@pytest.mark.asyncio
async def test_structured_model_call_rejects_invalid_model_json(tmp_path, monkeypatch):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config)
    registry = CapabilityRegistry(str(config))
    factory = _session(tmp_path, monkeypatch)
    _add_run(factory)
    context = authorize_broker_call(registry, "run-1", "painminer.tag")

    import src.capability_model_broker as broker

    monkeypatch.setattr(
        broker,
        "resolve_endpoint",
        lambda *args, **kwargs: ("http://model", "model-1", {}),
    )

    async def invalid_call(*args, **kwargs):
        return '{"answer":42}'

    monkeypatch.setattr(broker, "llm_call_async", invalid_call)

    with pytest.raises(CapabilityModelBrokerError, match="schema validation"):
        await invoke_structured_model(
            context,
            "prompt",
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        )


@pytest.mark.asyncio
async def test_structured_model_call_rejects_deep_schema_before_model_access(
    tmp_path, monkeypatch
):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config, max_schema_depth=2)
    registry = CapabilityRegistry(str(config))
    factory = _session(tmp_path, monkeypatch)
    _add_run(factory)
    context = authorize_broker_call(registry, "run-1", "painminer.tag")

    import src.capability_model_broker as broker

    async def must_not_call(*args, **kwargs):
        raise AssertionError("model should not be called")

    monkeypatch.setattr(broker, "llm_call_async", must_not_call)

    with pytest.raises(CapabilityModelRequestError, match="depth"):
        await invoke_structured_model(
            context,
            "prompt",
            {
                "type": "object",
                "properties": {
                    "nested": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    }
                },
            },
        )


@pytest.mark.asyncio
async def test_active_model_call_is_cancelled_when_capability_run_stops(
    tmp_path, monkeypatch
):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config)
    registry = CapabilityRegistry(str(config))
    factory = _session(tmp_path, monkeypatch)
    _add_run(factory)
    context = authorize_broker_call(registry, "run-1", "painminer.tag")

    import asyncio
    import src.capability_model_broker as broker

    started = asyncio.Event()
    cancelled = asyncio.Event()

    monkeypatch.setattr(
        broker,
        "resolve_endpoint",
        lambda *args, **kwargs: ("http://model", "model-1", {}),
    )

    async def slow_call(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(broker, "llm_call_async", slow_call)
    task = asyncio.create_task(
        invoke_structured_model(
            context,
            "prompt",
            {"type": "object", "properties": {}},
        )
    )
    await started.wait()
    db = factory()
    try:
        db.query(CapabilityRun).filter(CapabilityRun.id == "run-1").update(
            {"status": "cancelled"}
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(CapabilityModelAuthorizationError, match="cancelled"):
        await asyncio.wait_for(task, timeout=3)
    assert cancelled.is_set()
