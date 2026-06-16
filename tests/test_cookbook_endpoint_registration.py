from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COOKBOOK_RUNNING = ROOT / "static" / "js" / "cookbookRunning.js"


def _source() -> str:
    return COOKBOOK_RUNNING.read_text(encoding="utf-8")


def test_cookbook_marks_local_endpoint_registration_as_container_local():
    src = _source()
    assert "function _appendCookbookEndpointScope" in src
    assert "fd.append('container_local', 'true')" in src
    # Most served endpoints are now auto-registered by the backend
    # (_ensure_served_endpoint in src/tool_implementations.py, which sets
    # container_local itself); the frontend only registers as a fallback. The
    # invariant is that EVERY remaining frontend registration POST (the ones
    # that set skip_probe) is scoped via the helper, so a local serve is never
    # registered with a bare loopback URL the container can't reach.
    registration_posts = src.count("fd.append('skip_probe', 'true')")
    # Subtract the helper's own definition from the match count.
    scope_calls = src.count("_appendCookbookEndpointScope(fd,") - 1
    assert registration_posts >= 1
    assert scope_calls == registration_posts


def test_cookbook_does_not_use_local_as_endpoint_hostname():
    src = _source()
    assert "function _connectHostFromRemote" in src
    assert "if (!host || host === 'local') return fallback;" in src
    assert "const rawHost = task.remoteHost || 'localhost';" not in src


def test_cookbook_advertised_bind_urls_keep_connectable_host():
    src = _source()
    assert "function _endpointFromAdvertisedUrl" in src
    assert "_isAnyBindHost(u.hostname) ? currentHost" in src
    assert "host = u.hostname || host;" not in src


def test_parse_serve_phase_detects_native_llama_server_ready():
    """Native llama.cpp llama-server prints its own readiness lines, not
    uvicorn's "Application startup complete". _parseServePhase must flag those
    as ready or _serveReady never flips and the endpoint is never registered,
    so a healthy GPU model never reaches the picker.

    The native check must also precede the build-progress matcher so a finished
    build still in the snapshot tail can't pin the task at "building".
    """
    src = _source()
    assert "export function _parseServePhase" in src
    native = "if (/server is listening on|all slots are idle|llama_server: model loaded/i.test(flat))"
    assert native in src
    # Ordering: native-ready must come before the llama build-progress matcher.
    assert src.index(native) < src.index("const llamaBuildMatches =")
