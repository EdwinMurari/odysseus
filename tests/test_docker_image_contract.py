from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nvidia_overlay_requires_baked_cuda_llama_server():
    overlay = (ROOT / "docker" / "gpu.nvidia.yml").read_text(encoding="utf-8")

    assert 'LLAMA_CUDA: "on"' in overlay
    assert "ODYSSEUS_REQUIRE_CUDA_LLAMA_SERVER=1" in overlay


def test_entrypoint_enforces_nvidia_image_contract():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert '${ODYSSEUS_REQUIRE_CUDA_LLAMA_SERVER:-0}" = "1"' in entrypoint
    assert "command -v llama-server" in entrypoint
    assert "llama-server --version" in entrypoint
    assert "exit 78" in entrypoint
