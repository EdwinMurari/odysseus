# src/report_writer.py
"""Shared report-writing core — the single source of truth for *how* a report
is written, used by both the deep-research engine and capability runs.

The two report flows are deliberately **separate orchestrations**: deep research
drives an iterative Think→Search→Synthesize loop bound to an engine endpoint,
while capability runs synthesize from structured findings against an
owner-resolved endpoint (ADR-025). What they must NOT duplicate is the *report
quality mechanism* — the prose-style contract, the category format overrides,
the reasoning-tag strip, and the expand-if-too-short retry. Those live here,
once. Improve them here and both flows inherit the improvement; neither keeps a
private copy.

This module is stateless and has no notion of "search". Callers inject:
- ``framing``  — the task-specific prompt body (the question + evidence for deep
  research; the grounded structured findings for a capability),
- ``llm``      — an async callable ``(messages, **kw) -> str`` bound to whatever
  endpoint that flow uses.

``write_report`` appends the shared style/category requirements, calls the LLM,
strips thinking artifacts, and expands the draft if it came back too short.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from src.research_utils import strip_thinking


class LLMCallable(Protocol):
    """An async LLM call. Returns the raw model text (may include <think> tags;
    ``write_report`` strips them defensively, so injected callables need not)."""

    def __call__(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = ...,
        max_tokens: int = ...,
        timeout: int = ...,
    ) -> Awaitable[str]: ...


# ---------------------------------------------------------------------------
# Date grounding (shared primitive)
# ---------------------------------------------------------------------------
def current_date_context() -> str:
    """Preamble that grounds an LLM in the real current date. Without it the
    model falls back to its training-cutoff year. System TZ-local so it matches
    what the user sees. Portable strftime only."""
    now = datetime.now().astimezone()
    return (
        f"Today's date is {now.strftime('%B %d, %Y')} ({now.strftime('%Y-%m-%d')}). "
        f"When a search query needs a year or refers to 'latest'/'current'/"
        f"'this year', use {now.strftime('%Y')} or relative wording — never a "
        f"year inferred from training data.\n\n"
    )


# ---------------------------------------------------------------------------
# Shared report-writing style contract (single source of truth)
# ---------------------------------------------------------------------------
REPORT_STYLE_REQUIREMENTS = """\
- Use clear ## headings and ### subheadings to organize into logical sections
- Each section should have multiple detailed paragraphs, not just bullet points
- Synthesize and analyze the information — explain WHY things matter, draw comparisons, provide context
- Include specific data points, numbers, and statistics from the evidence
- Include source URLs as inline citations [like this](url)
- Note where sources agree and where they disagree
- Add a brief executive summary at the top
- End with a clear conclusion that directly answers the question
- Write in an engaging, informative style — not dry or robotic"""


# Per-category format overrides. Appended after the style requirements when a
# caller supplies a matching category. Shared so capability runs can opt into the
# same product/comparison/how-to/fact-check layouts deep research uses.
CATEGORY_PROMPTS = {
    "product": """IMPORTANT FORMAT OVERRIDE — this is a PRODUCT research report:
- Structure as a RANKED LIST of products/options (best first)
- For EACH product include: name as ### heading, approximate price, 2-3 sentence summary, **Pros:** bullet list, **Cons:** bullet list, **Where to buy:** URLs as links
- Start with a quick-compare markdown table of top picks (columns: Name, Price, Best For, Rating)
- End with a ## Verdict section picking Best Overall and Best Value
- Still include source citations inline""",

    "comparison": """IMPORTANT FORMAT OVERRIDE — this is a COMPARISON report:
- Create a ## Comparison Table as a markdown table comparing ALL options across key criteria (rows = criteria, columns = options)
- Use checkmarks, ratings, or short values in cells
- Write a ## section per option with its strengths, weaknesses, and ideal use case
- End with ## Best For verdicts (e.g., "**Best for small teams:** Option A because...")
- Include a ## Shared Considerations section for things that apply to all options""",

    "howto": """IMPORTANT FORMAT OVERRIDE — this is a HOW-TO guide:
- Start with ## Quick Guide — a super concise numbered list (one line per step, no details, just the action). Example: 1. Install X  2. Run Y  3. Configure Z
- Then ## Prerequisites listing what's needed before starting
- Then the detailed steps: ## Step 1: ..., ## Step 2: ...
- Each step should have a clear heading and detailed instructions
- Use blockquotes (> ) for tips and warnings: > **Tip:** ... or > **Warning:** ...
- End with ## Common Mistakes section
- Add estimated time and difficulty level near the top""",

    "factcheck": """IMPORTANT FORMAT OVERRIDE — this is a FACT-CHECK report:
- Start with ## The Claim restating what's being checked
- Create ## Evidence For and ## Evidence Against sections
- Each piece of evidence should be a ### with source name, what it found, and how strong the evidence is
- Include a ## Verdict section with one of: **Supported**, **Mixed Evidence**, or **Unsupported**
- End with ## Nuance & Caveats for important context and limitations
- Be balanced and cite sources for every claim""",
}


# Defaults: a heavy generation call. 180s matches the deep-research final-report
# timeout — a slow local model routinely needs >60s for it (#1551).
_REPORT_TIMEOUT_SECONDS = 180
_DEFAULT_MAX_TOKENS = 8192
_MIN_WORDS = 400

_EXPAND_INSTRUCTION = (
    "This report is too brief. Please expand it significantly:\n"
    "- Add detailed paragraphs for each section (not just bullet points)\n"
    "- Include specific data, numbers, and comparisons from the evidence\n"
    "- Explain context and significance — don't just list facts\n"
    "- Use ## headings and ### subheadings\n"
    "- Target at least 1000 words\n"
    "Write the full expanded report now."
)


def compose_report_prompt(
    framing: str,
    *,
    category: str | None = None,
    extra_requirements: str | None = None,
) -> str:
    """Assemble the final prompt: caller framing + shared style + category.

    Exposed separately so callers (and tests) can inspect exactly what will be
    sent, and so the style/category contract has one assembly point.
    """
    prompt = framing.rstrip() + "\n\nRequirements:\n"
    if extra_requirements:
        prompt += extra_requirements.rstrip() + "\n"
    prompt += REPORT_STYLE_REQUIREMENTS + "\n"
    cat_extra = CATEGORY_PROMPTS.get(category or "", "")
    if cat_extra:
        prompt += "\n\n" + cat_extra
    return prompt


async def write_report(
    *,
    framing: str,
    llm: LLMCallable,
    category: str | None = None,
    extra_requirements: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    expand_if_short: bool = True,
    min_words: int = _MIN_WORDS,
    emit: Callable[..., Any] | None = None,
) -> str:
    """Write a polished markdown report from ``framing`` using ``llm``.

    Owns the shared quality mechanism: style/category assembly, reasoning-tag
    strip, and an expand-if-too-short retry. Returns the report markdown, or an
    empty string if the model produced nothing. Does not catch model errors —
    the caller decides how to fall back (deep research keeps its evolving
    report; a capability falls back to its deterministic report).
    """
    prompt = compose_report_prompt(
        framing, category=category, extra_requirements=extra_requirements
    )

    result = strip_thinking(
        await llm(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=_REPORT_TIMEOUT_SECONDS,
        )
    )
    result = (result or "").strip()

    if expand_if_short and len(result.split()) < min_words and result:
        if emit:
            try:
                emit(phase="writing", message="Expanding report...")
            except Exception:
                pass
        expanded = strip_thinking(
            await llm(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": result},
                    {"role": "user", "content": _EXPAND_INSTRUCTION},
                ],
                temperature=0.4,
                max_tokens=max_tokens,
                timeout=_REPORT_TIMEOUT_SECONDS,
            )
        )
        expanded = (expanded or "").strip()
        if len(expanded.split()) > len(result.split()):
            result = expanded

    return result
