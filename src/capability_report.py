"""Report synthesis for capability runs — the deep-research approach.

Capabilities do the research and return structured, evidence-backed ``data``
in their result contract; Odysseus owns presentation. This module turns that
data into the same magazine-quality markdown report the deep-research engine
writes, using the owner's default model. It reuses the deep-research style
requirements verbatim (one source of truth) and is strictly grounded: the
model may only use what the capability returned.

Degradation: if no model endpoint is configured, the data block is missing,
or synthesis fails, the caller falls back to the capability's deterministic
report (``result["report"]``) — a run never loses its output because a model
was unavailable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.deep_research import REPORT_STYLE_REQUIREMENTS, current_date_context

logger = logging.getLogger(__name__)

# Structured results are compact relative to raw page text; this cap protects
# the context window of small local models while keeping full weekly runs
# (hundreds of findings) intact.
MAX_DATA_CHARS = 80_000
REPORT_TIMEOUT_SECONDS = 180
REPORT_MAX_TOKENS = 8192

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

Write a detailed, decision-ready research report of the findings.

Requirements:
""" + REPORT_STYLE_REQUIREMENTS + """

Write only the report markdown — no preamble or meta-commentary.
"""


def build_report_prompt(
    capability_name: str,
    capability_description: str,
    run_input: dict[str, Any],
    result: dict[str, Any],
) -> str | None:
    """The grounded synthesis prompt, or None when there is nothing to write from."""
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
    from src.endpoint_resolver import resolve_endpoint
    from src.llm_core import llm_call_async

    prompt = build_report_prompt(
        getattr(definition, "name", "capability"),
        getattr(definition, "description", ""),
        run_input,
        result,
    )
    if prompt is None:
        return None
    url, model, headers = resolve_endpoint("default", owner=owner)
    if not url or not model:
        logger.info(
            "Capability report synthesis skipped for %s: no default model endpoint",
            run_id,
        )
        return None
    content = await llm_call_async(
        url,
        model,
        [{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=REPORT_MAX_TOKENS,
        headers=headers,
        timeout=REPORT_TIMEOUT_SECONDS,
        prompt_type="capability:report",
        session_id=run_id,
    )
    content = (content or "").strip()
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
