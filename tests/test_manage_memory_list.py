import asyncio
from types import SimpleNamespace

from pathlib import Path


def test_memory_list_implementations_do_not_truncate_results():
    for path in ("mcp_servers/memory_server.py", "src/ai_interaction.py"):
        source = Path(path).read_text()
        assert "memories[:100]" not in source


def test_manage_memory_schema_allows_tidy():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    memory_schema = next(
        schema
        for schema in FUNCTION_TOOL_SCHEMAS
        if schema["function"]["name"] == "manage_memory"
    )

    assert "tidy" in memory_schema["function"]["parameters"]["properties"]["action"]["enum"]


def test_manage_memory_tidy_runs_existing_audit(monkeypatch):
    import services.memory.memory_extractor as memory_extractor
    import src.ai_interaction as ai
    import src.task_endpoint as task_endpoint

    memory_manager = object()
    memory_vector = object()
    session_headers = {"Authorization": "Bearer session"}
    session_manager = SimpleNamespace(
        get_session=lambda session_id: SimpleNamespace(
            owner="alice",
            endpoint_url="http://session.example/v1/chat/completions",
            model="session-model",
            headers=session_headers,
        )
    )
    resolver_calls = []
    audit_calls = []

    def fake_resolve_task_endpoint(
        fallback_url=None,
        fallback_model=None,
        fallback_headers=None,
        owner=None,
    ):
        resolver_calls.append((fallback_url, fallback_model, fallback_headers, owner))
        return fallback_url, fallback_model, fallback_headers

    async def fake_audit_memories(memory_manager_arg, memory_vector_arg, endpoint_url, model, headers, owner=None):
        audit_calls.append((memory_manager_arg, memory_vector_arg, endpoint_url, model, headers, owner))
        return {"before": 3, "after": 2}

    monkeypatch.setattr(ai, "_memory_manager", memory_manager)
    monkeypatch.setattr(ai, "_memory_vector", memory_vector)
    monkeypatch.setattr(ai, "_session_manager", session_manager)
    monkeypatch.setattr(task_endpoint, "resolve_task_endpoint", fake_resolve_task_endpoint)
    monkeypatch.setattr(memory_extractor, "audit_memories", fake_audit_memories)

    out = asyncio.run(ai.do_manage_memory("tidy", session_id="session-1", owner="alice"))

    assert resolver_calls == [(
        "http://session.example/v1/chat/completions",
        "session-model",
        session_headers,
        "alice",
    )]
    assert audit_calls == [(
        memory_manager,
        memory_vector,
        "http://session.example/v1/chat/completions",
        "session-model",
        session_headers,
        "alice",
    )]
    assert out["ok"] is True
    assert out["removed"] == 1
    assert out["results"] == "Tidied memories: 1 removed/merged (3 -> 2)."
