"""Regression coverage for llama.cpp startup OOMs caused by oversized context."""

from pathlib import Path

from routes.cookbook_helpers import _diagnose_serve_output


ROOT = Path(__file__).resolve().parents[1]


def test_serve_context_preserves_saved_value_and_defaults_to_8192():
    source = (ROOT / "static/js/cookbookServe.js").read_text(encoding="utf-8")

    assert "const _defaultCtx = String(Math.min(_modelCtxMax || 8192, 8192));" in source
    assert 'value="${esc(sv(\'ctx\', _defaultCtx))}"' in source
    assert "resets to the model max on every open" not in source


def test_serve_context_is_clamped_to_hardware_safe_limit():
    source = (ROOT / "static/js/cookbookServe.js").read_text(encoding="utf-8")

    assert "panel._safeCtxMax" in source
    assert "data.safe_ctx_max" in source
    assert "Math.min(...caps)" in source


def test_linux_llama_launch_does_not_fallback_after_native_runtime_error():
    source = (ROOT / "static/js/cookbook.js").read_text(encoding="utf-8")

    assert "if command -v llama-server >/dev/null 2>&1; then" in source
    assert "cmd += ` || ${_lcpServer}`;" not in source


def test_backend_diagnoses_sigkill_137_as_host_memory_exhaustion():
    output = """
    llama_kv_cache: CPU KV buffer size = 21760.00 MiB
    /tmp/odysseus-tmux/serve.sh: line 104: 2773 Killed
      python3 -m llama_cpp.server --n_ctx 131072
    === Process exited with code 137 ===
    """

    diagnosis = _diagnose_serve_output(output)

    assert diagnosis is not None
    assert "system memory" in diagnosis["message"].lower()
    assert diagnosis["suggestions"][0] == {
        "label": "retry with context 8192",
        "op": "settings",
        "field": "ctx",
        "value": "8192",
    }


def test_browser_diagnosis_has_sigkill_137_rule():
    source = (ROOT / "static/js/cookbook-diagnosis.js").read_text(encoding="utf-8")

    assert "Process exited with code\\s+137" in source
    assert "operating system killed the server after it exhausted system memory" in source
