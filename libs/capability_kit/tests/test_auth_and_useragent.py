import pytest

from capability_kit.auth import verify_bearer
from capability_kit.useragent import (
    Identity,
    UserAgentError,
    browser_user_agent,
    default_headers,
    default_user_agent,
    edgar_user_agent,
    identity_from_env,
    reddit_user_agent,
)

ANON = Identity(app="painminer", version="0.1")
WITH_CONTACT = Identity(app="painminer", version="0.1", contact="Edwin edwin@example.com")


def test_verify_bearer_accepts_exact_match_only():
    assert verify_bearer("Bearer secret", "secret")
    assert not verify_bearer("Bearer wrong", "secret")
    assert not verify_bearer("secret", "secret")
    assert not verify_bearer(None, "secret")


def test_verify_bearer_never_passes_without_configured_token():
    assert not verify_bearer("Bearer ", "")
    assert not verify_bearer(None, "")


def test_default_user_agent_is_bare_product_token():
    """Industry standard for API clients: app/version, no personal details."""
    assert default_user_agent(ANON) == "painminer/0.1"
    assert default_user_agent(WITH_CONTACT) == "painminer/0.1"  # contact NOT exposed


def test_default_user_agent_may_carry_project_url():
    identity = Identity(app="painminer", version="0.1", url="https://example.com/painminer")
    assert default_user_agent(identity) == "painminer/0.1 (+https://example.com/painminer)"


def test_edgar_user_agent_is_bare_name_email():
    assert edgar_user_agent(WITH_CONTACT) == "Edwin edwin@example.com"


def test_edgar_user_agent_fails_loud_without_contact():
    with pytest.raises(UserAgentError, match="CAPABILITY_CONTACT"):
        edgar_user_agent(ANON)


def test_browser_user_agent_is_full_browser_profile_with_app_token():
    """WAF bot rules block 'Mozilla/5.0 (compatible; ...)'; a real browser
    profile with a trailing app token passes while staying attributable."""
    ua = browser_user_agent(WITH_CONTACT)
    assert ua.startswith("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    assert "Chrome/" in ua and "Safari/" in ua
    assert ua.endswith("painminer/0.1")
    assert "compatible;" not in ua  # the blocked crawler signature
    assert "@" not in ua  # contact never leaks into the browser token


def test_reddit_user_agent_follows_api_rules():
    assert reddit_user_agent(ANON, "pm_bot") == "python:painminer:v0.1 (by /u/pm_bot)"


def test_reddit_user_agent_requires_bot_username():
    with pytest.raises(UserAgentError):
        reddit_user_agent(ANON, "")


def test_identity_validates_contact_when_provided():
    with pytest.raises(UserAgentError):
        Identity(app="x", version="1", contact="no-email-here")


def test_identity_from_env_reads_canonical_var(monkeypatch):
    monkeypatch.setenv("CAPABILITY_CONTACT", "Edwin edwin@example.com")
    identity = identity_from_env("painminer", "0.1")
    assert identity.contact == "Edwin edwin@example.com"


def test_identity_from_env_falls_back_to_edgar_var(monkeypatch):
    monkeypatch.delenv("CAPABILITY_CONTACT", raising=False)
    monkeypatch.setenv("EDGAR_USER_AGENT", "Edwin edwin@example.com")
    identity = identity_from_env("stock-research", "0.1")
    assert identity.contact == "Edwin edwin@example.com"


def test_identity_from_env_is_anonymous_when_unset(monkeypatch):
    monkeypatch.delenv("CAPABILITY_CONTACT", raising=False)
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    identity = identity_from_env("painminer", "0.1")
    assert identity.contact is None


def test_identity_from_env_can_require_contact(monkeypatch):
    monkeypatch.delenv("CAPABILITY_CONTACT", raising=False)
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    with pytest.raises(UserAgentError, match="CAPABILITY_CONTACT"):
        identity_from_env("stock-research", "0.1", require_contact=True)


def test_default_headers_contains_simple_user_agent_only():
    assert default_headers(WITH_CONTACT) == {"User-Agent": "painminer/0.1"}
