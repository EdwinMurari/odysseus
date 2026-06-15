"""Weak-model output handling — the single discipline every capability uses
when it lets a local model write prose for a report.

Why this lives in the kit (ADR-002, DRY): capabilities run as separate worker
processes that CANNOT import Odysseus internals (``src.text_helpers``,
``src.research_utils``). Before this module, a capability that wanted clean
model prose had to re-implement thinking-strip + tolerant extraction itself —
exactly the cross-repo duplication the kit exists to prevent. Now every worker
shares one implementation.

Scope note: Odysseus's own in-process paths use the richer
``src.text_helpers.strip_think`` (it covers many model dialects — Gemma
channels, Qwen "Thinking Process", prompt-echo). This kit port covers the core
cases a capability worker realistically sees from the broker — tagged
``<think>``/``<thinking>``/``<thought>`` blocks (closed, nested, orphan, or a
dangling opener). The two share a contract, not code, because they live on
opposite sides of a process boundary.

The three pieces, used together by ``BrokerClient.narrative_text``:

- ``strip_thinking``  — remove reasoning blocks the model emits before its answer
- ``coerce_text``     — pull the prose out whether the model returned JSON, a
                        bare string, or JSON-wrapped-in-prose (weak models are
                        unreliable at honoring a schema)
- ``contains_digits`` — a validator capabilities use to reject any model prose
                        that invented a figure (the report's numbers are
                        deterministic; narrative prose must carry none)
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["strip_thinking", "coerce_text", "contains_digits"]

# Tag name covers <think>, <thinking>, <thought> (and attribute variants).
_TAG = r"(?:think(?:ing)?|thought)"
# Closed block. The body is a tempered pattern: it may contain anything EXCEPT
# another opening tag, so this matches the INNERMOST block first. `strip_thinking`
# loops the substitution, unwinding nested `<think><think>..</think>..</think>`
# from the inside out — a plain non-greedy `.*?` would instead span from the
# outer opener to the inner closer and leave the rest mangled.
_CLOSED_RE = re.compile(
    rf"<{_TAG}(?:\s+[^>]*)?>(?:(?!<{_TAG})[\s\S])*?</{_TAG}>\s*",
    re.IGNORECASE,
)
# Dangling opener with no closer: strip from the opener to end-of-string.
_OPEN_RE = re.compile(rf"<{_TAG}(?:\s+[^>]*)?>[\s\S]*$", re.IGNORECASE)
# Orphan opening/closing tags left after the passes above.
_ORPHAN_RE = re.compile(rf"</?{_TAG}[^>]*>\s*", re.IGNORECASE)

_DIGIT_RE = re.compile(r"\d")


def strip_thinking(text: str | None) -> str:
    """Remove ``<think>``/``<thinking>``/``<thought>`` reasoning blocks.

    Preserves a ``None`` passthrough as an empty string so callers can treat a
    failed/empty model call uniformly. Idempotent.
    """
    if not text:
        return ""
    prev = None
    # Loop the closed-block pass to unwind nested blocks (innermost-first).
    while prev != text:
        prev = text
        text = _CLOSED_RE.sub("", text)
    # A dangling opener (model never closed the block) -> drop to end of string.
    text = _OPEN_RE.sub("", text)
    # Any remaining orphan tags.
    text = _ORPHAN_RE.sub("", text)
    return text.strip()


def coerce_text(data: Any, key: str = "narrative") -> str:
    """Extract prose from whatever a weak model returned.

    Tolerant by design (the model often ignores the requested schema):
    - a ``dict`` -> ``data[key]`` (falls back to ``data["text"]``),
    - a bare ``str`` -> itself,
    - a JSON object embedded in a string -> parsed, then ``[key]``/``["text"]``.

    Returns ``""`` when nothing usable is present.
    """
    if isinstance(data, dict):
        return str(data.get(key) or data.get("text") or "").strip()
    if isinstance(data, str):
        s = data.strip()
        if s.startswith("{") and (key in s or "text" in s):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return str(obj.get(key) or obj.get("text") or "").strip()
            except (ValueError, TypeError):
                pass
        return s
    return ""


def contains_digits(text: str) -> bool:
    """True if the text contains any digit.

    Capabilities use this to reject narrative prose that invented a figure:
    when the model is asked to write only qualitatively (all real numbers stay
    in the deterministic report), a compliant reply contains no digits at all,
    so any digit means an unverifiable value slipped in.
    """
    return bool(text) and bool(_DIGIT_RE.search(text))
