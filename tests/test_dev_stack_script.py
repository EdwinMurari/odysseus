from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_logs_are_bounded_unless_follow_is_explicit():
    script = (ROOT / "dev-stack.ps1").read_text(encoding="utf-8")

    assert "[int]$LogTail = 200" in script
    assert "[switch]$Follow" in script
    assert "if ($FollowOutput)" in script
    assert '$arguments += "--follow"' in script
    assert "Show-ComposeLogs -Tail $LogTail -FollowOutput:$Follow" in script
    assert "logs --tail=200 -f" not in script


def test_compose_commands_use_the_shared_checked_helper():
    script = (ROOT / "dev-stack.ps1").read_text(encoding="utf-8")

    assert "function Invoke-Compose" in script
    assert "function Show-ComposeLogs" in script
    assert "Invoke-Compose @(" not in script
    assert "& docker compose @script:ComposeArgs" not in script
    assert "Invoke-Checked docker compose @script:ComposeArgs" not in script
