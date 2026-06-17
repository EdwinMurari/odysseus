from pathlib import Path


def test_memory_tidy_posts_active_session_for_model_fallback():
    source = Path("static/js/memory.js").read_text(encoding="utf-8")

    assert "new FormData()" in source
    assert "window.sessionModule?.getCurrentSessionId?.()" in source
    assert "form.append('session', sessionId)" in source
    assert "body: form" in source
