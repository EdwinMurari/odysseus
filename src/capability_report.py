"""Report synthesis for capability runs — its own flow, shared quality core.

Capabilities do the research and return structured, evidence-backed ``data``
in their result contract; Odysseus owns presentation. This module is a
*separate orchestration* from the deep-research engine (it resolves the owner's
endpoint per ADR-025 and synthesizes from structured findings rather than a
search loop), but it writes the actual report through the same shared core,
``src.report_writer.write_report``. That core owns the style contract, category
overrides, thinking-strip, and the expand-if-too-short retry — so improvements
to deep-research report quality reach capability reports automatically, with no
private copy here.

The synthesis is strictly grounded: the model may only use what the capability
returned.

Degradation: if no model endpoint is configured, the data block is missing,
or synthesis fails, the caller falls back to the capability's deterministic
report (``result["report"]``) — a run never loses its output because a model
was unavailable.

Opt-out: a capability may set ``result["report_is_final"]`` to keep its own
report verbatim and skip JSON-dump re-synthesis entirely. This exists for
capabilities whose reports carry exact numbers that must never be re-narrated
by a weak local model (hallucination risk); such a capability does any LLM
polish itself, on its own side, where it can constrain the model safely.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.report_writer import current_date_context, write_report

logger = logging.getLogger(__name__)

# Structured results are compact relative to raw page text; this cap protects
# the context window of small local models while keeping full weekly runs
# (hundreds of findings) intact.
MAX_DATA_CHARS = 80_000
REPORT_TIMEOUT_SECONDS = 180
REPORT_MAX_TOKENS = 8192

# Capability-specific requirement, inserted ahead of the shared style contract
# by write_report's extra_requirements hook.
_CAPABILITY_REPORT_EXTRA = (
    "- Write only the report markdown — no preamble or meta-commentary."
)

# The task framing only — the shared style/category requirements are appended by
# report_writer.write_report so they stay a single source of truth.
CAPABILITY_REPORT_PROMPT = """\
{date_context}You are writing the report for a completed "{capability_name}" research run.
{capability_description}

**Run parameters:**
{run_input}

**Run summary:** {summary}

**Structured findings (the complete evidence — your ONLY source of facts):**
```json
{data}
```

Grounding rules (non-negotiable):
- Every fact, number, score, quote, and URL must come from the structured
  findings above. Never invent, extrapolate, or pull in outside knowledge.
- Cite the evidence URLs from the findings as inline links where relevant.
- If the findings include warnings, failed sources, or partial coverage,
  state them plainly in a "Coverage & limitations" section — do not bury or
  soften them.
- If the findings are empty or insufficient, say so directly and describe
  what ran and what failed instead of padding.

Write a detailed, decision-ready research report of the findings."""


def build_report_prompt(
    capability_name: str,
    capability_description: str,
    run_input: dict[str, Any],
    result: dict[str, Any],
) -> str | None:
    """The grounded synthesis *framing*, or None when there is nothing to write
    from. The shared style/category requirements are added later by
    ``report_writer.write_report``."""
    data = result.get("data")
    if not isinstance(data, dict) or not data:
        return None
    payload = dict(data)
    # Surface run-level context the capability reported alongside the data.
    for key in ("metrics", "warnings", "source_failures", "failures"):
        value = result.get(key)
        if value and key not in payload:
            payload[key] = value
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(encoded) > MAX_DATA_CHARS:
        encoded = encoded[:MAX_DATA_CHARS] + "\n... (truncated)"
    return CAPABILITY_REPORT_PROMPT.format(
        date_context=current_date_context(),
        capability_name=capability_name,
        capability_description=capability_description or "",
        run_input=json.dumps(run_input or {}, ensure_ascii=False, sort_keys=True),
        summary=str(result.get("summary") or "(none provided)"),
        data=encoded,
    )


async def synthesize_capability_report(
    definition: Any,
    run_id: str,
    run_input: dict[str, Any],
    result: dict[str, Any],
    owner: str | None,
) -> dict[str, str] | None:
    """Write the run report from structured findings.

    Returns ``{"title", "format", "content"}`` plus model provenance under
    ``"provenance"``, or ``None`` when synthesis is not possible (no data, no
    endpoint). Raises on model failure so the caller can record the warning
    and fall back to the capability's deterministic report.
    """
    # A capability may declare its own report authoritative — e.g. one whose
    # numbers must never be re-narrated by a weak local model (hallucination
    # risk). When ``report_is_final`` is set, Odysseus keeps the capability's
    # report verbatim and does NOT run JSON-dump re-synthesis. The capability is
    # then responsible for any LLM polish it wants, done safely on its own side.
    # Checked first so a final report never even imports the model stack.
    if result.get("report_is_final"):
        logger.info(
            "Capability report synthesis skipped for %s: report_is_final set",
            run_id,
        )
        return None

    from src.endpoint_resolver import resolve_endpoint
    from src.llm_core import llm_call_async

    framing = build_report_prompt(
        getattr(definition, "name", "capability"),
        getattr(definition, "description", ""),
        run_input,
        result,
    )
    if framing is None:
        return None
    url, model, headers = resolve_endpoint("default", owner=owner)
    if not url or not model:
        logger.info(
            "Capability report synthesis skipped for %s: no default model endpoint",
            run_id,
        )
        return None

    async def _llm(
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = REPORT_MAX_TOKENS,
        timeout: int = REPORT_TIMEOUT_SECONDS,
    ) -> str:
        """The owner-resolved endpoint, bound for the shared report writer."""
        return await llm_call_async(
            url,
            model,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            headers=headers,
            timeout=timeout,
            prompt_type="capability:report",
            session_id=run_id,
        )

    # Delegate the actual writing to the shared core: it appends the style
    # contract, applies any category override, strips thinking, and expands a
    # too-short draft — the same quality mechanism deep research uses.
    content = (
        await write_report(
            framing=framing,
            llm=_llm,
            category=str(result.get("report_category") or "") or None,
            extra_requirements=_CAPABILITY_REPORT_EXTRA,
            max_tokens=REPORT_MAX_TOKENS,
        )
    ).strip()
    if not content:
        raise RuntimeError("model returned an empty report")
    report_meta = result.get("report") if isinstance(result.get("report"), dict) else {}
    title = str(
        result.get("report_title")
        or report_meta.get("title")
        or f"{getattr(definition, 'name', 'Capability')} report"
    )
    return {
        "title": title,
        "format": "markdown",
        "content": content,
        "provenance": {
            "role": "capability.report",
            "setting_prefix": "default",
            "model": model,
            "endpoint": url,
        },
    }


# ---------------------------------------------------------------------------
# Visual (HTML) rendering — the same magazine-style view deep research produces.
#
# Deep research and capability runs share the report *renderer*
# (src/visual_report.generate_visual_report) just as they share the report
# *writer*. The capability report is stored as a markdown Document; this renders
# that markdown to HTML on demand, so improvements to the renderer reach both
# flows automatically (no private copy).
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")


def extract_markdown_sources(markdown: str) -> list[dict[str, str]]:
    """Best-effort source list for the visual report's Sources panel, built from
    the report's own inline citations. Deduplicated by URL, order preserved.

    Capability reports are grounded in the citations the writer emitted, so the
    report markdown is the authoritative source of links — no structured ``data``
    needs to be persisted to populate the panel.
    """
    seen: list[dict[str, str]] = []
    known: set[str] = set()
    for url in _MD_LINK_RE.findall(markdown or ""):
        url = url.rstrip(".,);")
        if url and url not in known:
            known.add(url)
            seen.append({"url": url})
    return seen


def render_report_html(
    title: str,
    markdown: str,
    *,
    category: str | None = None,
) -> str:
    """Render a capability report as visual HTML via the shared renderer.

    Sources are derived from the report's inline citations; the deep-research
    stats bar (Duration/Rounds/URLs/Model) does not apply to capability runs and
    is intentionally left empty.
    """
    from src.visual_report import generate_visual_report

    return generate_visual_report(
        question=title or "Capability report",
        report_markdown=markdown or "",
        sources=extract_markdown_sources(markdown),
        stats={},
        category=category,
    )
