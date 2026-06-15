"""Capability report synthesis: grounded prompt, fallback behavior, runner wiring."""

import asyncio
from dataclasses import dataclass

import pytest

from src.capability_report import (
    MAX_DATA_CHARS,
    build_report_prompt,
    extract_markdown_sources,
    synthesize_capability_report,
)
# The style contract is the shared single source of truth in report_writer;
# build_report_prompt returns only the framing, and write_report appends the
# style/category requirements (verified via compose_report_prompt below).
from src.report_writer import REPORT_STYLE_REQUIREMENTS, compose_report_prompt


@dataclass
class _Definition:
    name: str = "Pain Miner"
    description: str = "Build an evidence-backed paid-pain report."


RESULT = {
    "summary": "3 clusters from 412 signals.",
    "data": {
        "findings": [
            {
                "title": "AU payroll SaaS pain",
                "total_score": 18,
                "urls": ["https://example.com/post/1"],
            }
        ],
        "pipeline_health": [{"source": "hn", "status": "success"}],
    },
    "metrics": {"clusters": 3},
    "warnings": ["g2 failed: 403"],
}


def test_prompt_is_grounded_in_capability_data():
    prompt = build_report_prompt(
        "Pain Miner", "desc", {"since": "7d"}, RESULT
    )
    assert "AU payroll SaaS pain" in prompt
    assert "https://example.com/post/1" in prompt
    assert "g2 failed: 403" in prompt  # warnings ride along for the coverage section
    assert '"since": "7d"' in prompt
    assert "ONLY source of facts" in prompt
    # The framing carries the grounding; the shared style contract is appended
    # by the report writer (same single source of truth as deep research).
    assert REPORT_STYLE_REQUIREMENTS not in prompt
    assert REPORT_STYLE_REQUIREMENTS in compose_report_prompt(prompt)


def test_prompt_requires_structured_data():
    assert build_report_prompt("X", "", {}, {"summary": "ok"}) is None
    assert build_report_prompt("X", "", {}, {"data": {}}) is None
    assert build_report_prompt("X", "", {}, {"data": "not a dict"}) is None


def test_prompt_truncates_oversized_data():
    result = {"data": {"blob": "x" * (MAX_DATA_CHARS * 2)}}
    prompt = build_report_prompt("X", "", {}, result)
    assert "(truncated)" in prompt
    assert len(prompt) < MAX_DATA_CHARS + 5_000


def test_synthesis_skips_without_endpoint(monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda prefix, owner=None: (None, None, None),
    )
    out = asyncio.run(
        synthesize_capability_report(_Definition(), "run-1", {}, RESULT, "admin")
    )
    assert out is None


def test_synthesis_skips_without_data(monkeypatch):
    def _boom(*args, **kwargs):  # endpoint must not even be resolved
        raise AssertionError("resolve_endpoint should not be called")

    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", _boom)
    out = asyncio.run(
        synthesize_capability_report(
            _Definition(), "run-1", {}, {"summary": "no data"}, "admin"
        )
    )
    assert out is None


def test_synthesis_skips_when_report_is_final(monkeypatch):
    """A capability that marks its report final keeps it verbatim — Odysseus
    runs no JSON-dump re-synthesis (so a weak model can't re-narrate the
    numbers). The endpoint is never even resolved."""
    def _boom(*args, **kwargs):
        raise AssertionError("resolve_endpoint should not be called")

    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", _boom)
    result = dict(RESULT, report_is_final=True)
    out = asyncio.run(
        synthesize_capability_report(_Definition(), "run-1", {}, result, "admin")
    )
    assert out is None


def test_synthesis_returns_report_with_provenance(monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda prefix, owner=None: ("http://llm:8000", "test-model", {}),
    )

    async def _fake_llm(url, model, messages, **kwargs):
        assert "ONLY source of facts" in messages[0]["content"]
        return "## Executive summary\n\nReport body."

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_llm)
    out = asyncio.run(
        synthesize_capability_report(_Definition(), "run-1", {}, dict(RESULT), "admin")
    )
    assert out["format"] == "markdown"
    assert out["content"].startswith("## Executive summary")
    assert out["title"] == "Pain Miner report"
    assert out["provenance"]["model"] == "test-model"
    assert out["provenance"]["role"] == "capability.report"


def test_synthesis_prefers_capability_report_title(monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda prefix, owner=None: ("http://llm:8000", "test-model", {}),
    )

    async def _fake_llm(url, model, messages, **kwargs):
        return "body"

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_llm)
    result = dict(RESULT, report_title="Pain Mining Digest - 2026-06-13")
    out = asyncio.run(
        synthesize_capability_report(_Definition(), "run-1", {}, result, "admin")
    )
    assert out["title"] == "Pain Mining Digest - 2026-06-13"


def test_synthesis_raises_on_empty_model_reply(monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda prefix, owner=None: ("http://llm:8000", "test-model", {}),
    )

    async def _fake_llm(url, model, messages, **kwargs):
        return "   "

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_llm)
    with pytest.raises(RuntimeError):
        asyncio.run(
            synthesize_capability_report(_Definition(), "run-1", {}, dict(RESULT), "admin")
        )


def test_extract_markdown_sources_dedupes_and_preserves_order():
    # The visual report's Sources panel is built from the report's own inline
    # citations — deduplicated by URL, first occurrence order preserved.
    md = (
        "Intro [a](https://ex.com/a), more [a again](https://ex.com/a), "
        "and [b](https://x.io/b)."
    )
    assert extract_markdown_sources(md) == [
        {"url": "https://ex.com/a"},
        {"url": "https://x.io/b"},
    ]


def test_extract_markdown_sources_handles_empty():
    assert extract_markdown_sources("") == []
    assert extract_markdown_sources(None) == []
