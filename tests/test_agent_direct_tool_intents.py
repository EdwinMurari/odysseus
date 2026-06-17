import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _events(chunks):
    parsed = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                parsed.append(json.loads(chunk[6:]))
            except Exception:
                pass
    return parsed


def test_direct_memory_tidy_executes_without_llm(monkeypatch):
    exec_calls = []

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(
        al,
        "_build_system_prompt",
        lambda messages, *a, **k: (messages, []),
        raising=False,
    )

    async def should_not_stream(*args, **kwargs):
        raise AssertionError("explicit tidy memory intent should not call the LLM")
        yield

    async def fake_execute(block, *args, **kwargs):
        exec_calls.append((block, kwargs))
        return ("manage_memory: tidy", {"results": "Already clean.", "exit_code": 0})

    monkeypatch.setattr(al, "stream_llm_with_fallback", should_not_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", fake_execute, raising=False)

    chunks = _collect(
        al.stream_agent_loop(
            "https://chatgpt.com/backend-api/codex/responses",
            "gpt-5.5",
            [{"role": "user", "content": "Please tidy brain memories"}],
            max_rounds=2,
            relevant_tools={"manage_memory"},
            session_id="session-1",
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    assert len(exec_calls) == 1
    assert exec_calls[0][0].tool_type == "manage_memory"
    assert exec_calls[0][0].content == "tidy"
    assert exec_calls[0][1]["session_id"] == "session-1"
    assert any(
        event.get("type") == "tool_start" and event.get("tool") == "manage_memory"
        for event in events
    )
    assert any(
        event.get("type") == "tool_output"
        and event.get("tool") == "manage_memory"
        and event.get("output") == "Already clean."
        for event in events
    )
    assert any(event.get("delta") == "Already clean." for event in events)


def test_direct_memory_tidy_does_not_catch_open_memory_panel():
    assert al._direct_tool_request("open brain memories") is None
