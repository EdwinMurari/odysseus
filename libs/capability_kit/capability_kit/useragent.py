"""User-Agent policy: simple by default, identified only where required.

Industry convention for API clients is a bare product token --
``app/version`` -- like ``curl/8.6`` or ``python-httpx/0.27``. Personal
contact details go into a User-Agent only when the target service's policy
demands them:

- default:  ``app/version`` -- every ordinary API call. No contact.
- EDGAR:    ``Name email`` -- SEC fair-access policy hard requirement.
- browser:  ``Mozilla/5.0 (compatible; app/version)`` -- for CDN fronts
  (e.g. CloudFront) that reject non-browser UAs. No contact.
- Reddit:   ``platform:app:vversion (by /u/bot)`` -- Reddit API rules; needs
  the bot account name (credential-specific argument), never an email.

The identity's ``contact`` is therefore optional; builders that require it
(``edgar_user_agent``) fail loud when it is absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

CONTACT_ENV = "CAPABILITY_CONTACT"


class UserAgentError(ValueError):
    """Raised when the outbound identity is missing or malformed."""


@dataclass(frozen=True)
class Identity:
    app: str
    version: str
    contact: str | None = None  # "Name email" -- only for services that demand it
    url: str | None = None

    def __post_init__(self) -> None:
        if not self.app or "/" in self.app:
            raise UserAgentError("Identity.app must be a non-empty token without '/'")
        if not self.version:
            raise UserAgentError("Identity.version must be non-empty")
        if self.contact is not None and "@" not in self.contact:
            raise UserAgentError(
                "Identity.contact must be 'Name email' with a reachable address"
            )


def identity_from_env(
    app: str,
    version: str,
    *,
    env: str = CONTACT_ENV,
    fallback_envs: tuple[str, ...] = ("EDGAR_USER_AGENT",),
    url: str | None = None,
    require_contact: bool = False,
) -> Identity:
    """Build the identity, reading the optional contact from the environment.

    ``CAPABILITY_CONTACT`` is the canonical variable; ``EDGAR_USER_AGENT``
    is accepted as a fallback because it carries the identical "Name email"
    payload and predates the kit. Contact is optional unless
    ``require_contact`` is set (use for services whose policy demands it).
    """
    for name in (env, *fallback_envs):
        value = os.getenv(name, "").strip().strip('"')
        if value:
            if "@" not in value:
                raise UserAgentError(
                    f"{name} must be 'Name email' with a reachable address"
                )
            return Identity(app=app, version=version, contact=value, url=url)
    if require_contact:
        raise UserAgentError(
            f"Missing outbound identity: set {env}='Your Name your@email'"
        )
    return Identity(app=app, version=version, url=url)


def default_user_agent(identity: Identity) -> str:
    """Bare product token -- no personal details."""
    if identity.url:
        return f"{identity.app}/{identity.version} (+{identity.url})"
    return f"{identity.app}/{identity.version}"


def edgar_user_agent(identity: Identity) -> str:
    """SEC EDGAR mandates a bare 'Name email' User-Agent."""
    if not identity.contact:
        raise UserAgentError(
            "EDGAR requires a contact identity: set "
            f"{CONTACT_ENV} (or EDGAR_USER_AGENT) as 'Your Name your@email'"
        )
    return identity.contact


# A current, real browser profile. WAF bot rules (AWS WAF / CloudFront
# managed rules in particular) block the classic "Mozilla/5.0 (compatible;
# app/1.0)" crawler pattern outright — AusTender's ATM pages answered 403 to
# it in production. A full browser profile with the app token appended as a
# trailing product token passes those rules while keeping the client
# identifiable in server logs. No personal details either way.
_BROWSER_PROFILE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def browser_user_agent(identity: Identity) -> str:
    """Browser-profile UA for CDN/WAF-fronted sites -- no personal details.

    The app token rides as a trailing product token (the convention tools
    like Electron apps use), so the request is still attributable without
    matching WAF crawler signatures.
    """
    token = f"{identity.app}/{identity.version}"
    if identity.url:
        token += f" (+{identity.url})"
    return f"{_BROWSER_PROFILE} {token}"


def reddit_user_agent(identity: Identity, bot_username: str, platform: str = "python") -> str:
    if not bot_username:
        raise UserAgentError("Reddit user agents require the bot account username")
    return f"{platform}:{identity.app}:v{identity.version} (by /u/{bot_username.lstrip('/u/')})"


def default_headers(identity: Identity) -> dict[str, str]:
    return {"User-Agent": default_user_agent(identity)}
