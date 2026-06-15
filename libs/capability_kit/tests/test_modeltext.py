"""Weak-model output handling (ADR-004 support): strip_thinking, coerce_text,
contains_digits, and BrokerClient.narrative_text — the shared discipline every
capability uses for model-written prose."""

import httpx

from capability_kit import BrokerClient, coerce_text, contains_digits, strip_thinking


# --- strip_thinking --------------------------------------------------------

def test_strip_closed_think_block():
    assert strip_thinking("<think>reasoning</think>Answer.") == "Answer."


def test_strip_handles_thinking_and_thought_variants():
    assert strip_thinking("<thinking>x</thinking>\n\nReal answer.") == "Real answer."
    assert strip_thinking("<thought>y</thought>Done.") == "Done."


def test_strip_nested_blocks():
    assert strip_thinking("<think><think>a</think>b</think>C.") == "C."


def test_strip_dangling_opener_drops_to_end():
    # No closing tag: everything from the opener is reasoning -> gone.
    assert strip_thinking("<think>unfinished reasoning that never closes") == ""


def test_strip_none_and_empty():
    assert strip_thinking(None) == ""
    assert strip_thinking("") == ""


def test_strip_is_idempotent():
    once = strip_thinking("<think>r</think>Final.")
    assert strip_thinking(once) == once == "Final."


# --- coerce_text -----------------------------------------------------------

def test_coerce_dict_with_key():
    assert coerce_text({"narrative": "hi"}) == "hi"
    assert coerce_text({"text": "fallback"}) == "fallback"


def test_coerce_bare_string():
    assert coerce_text("just prose") == "just prose"


def test_coerce_json_wrapped_in_string():
    assert coerce_text('{"narrative": "wrapped prose"}') == "wrapped prose"


def test_coerce_custom_key():
    assert coerce_text({"summary": "s"}, key="summary") == "s"


def test_coerce_garbage_returns_empty():
    assert coerce_text(None) == ""
    assert coerce_text(123) == ""


# --- contains_digits -------------------------------------------------------

def test_contains_digits():
    assert contains_digits("up 5 percent") is True
    assert contains_digits("trades at 50x") is True
    assert contains_digits("strong momentum, rich valuation") is False
    assert contains_digits("") is False


# --- BrokerClient.narrative_text -------------------------------------------

def _client(handler):
    return BrokerClient(
        "http://broker", "tok", "run-1", "stockresearch.deepdive",
        transport=httpx.MockTransport(handler),
    )


def test_narrative_text_strips_and_coerces():
    def handler(request):
        return httpx.Response(200, json={
            "data": {"narrative": "<think>hmm</think>Solid quality, rich price."},
            "provenance": {},
        })

    out = _client(handler).narrative_text("write it")
    assert out == "Solid quality, rich price."


def test_narrative_text_tolerates_bare_string_data():
    def handler(request):
        return httpx.Response(200, json={"data": "Plain prose reply.", "provenance": {}})

    assert _client(handler).narrative_text("x") == "Plain prose reply."
