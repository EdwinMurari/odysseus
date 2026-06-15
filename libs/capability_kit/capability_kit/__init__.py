"""Shared contracts for Odysseus capability workers.

One package, the cross-repo concerns, each previously duplicated across repos:

- ``auth``      constant-time bearer-token verification
- ``broker``    client for the Odysseus capability-model broker
- ``env``       env reading that tolerates dotenv-style quoting (compose
                ``format: raw`` passes quotes through verbatim)
- ``modeltext`` weak-model output handling: strip thinking tags, tolerantly
                coerce to prose, detect invented figures (ADR-004 support)
- ``useragent`` single-identity User-Agent policy (default / EDGAR / browser / Reddit)
- ``worker``    the v1 run-lifecycle HTTP protocol (FastAPI; requires the
                ``worker`` extra) and ``runs`` stores backing it

The kit is the single source of truth for the worker protocol Odysseus's
``capability_runner`` consumes: POST /v1/runs -> {"id"}, GET status ->
{"status", "result", "error"}, GET log -> {"output"}, DELETE cancel,
GET /v1/readiness -> {"ready", "dependencies"}.
"""

from capability_kit.auth import verify_bearer
from capability_kit.broker import BrokerClient, BrokerError
from capability_kit.env import getenv as env_getenv
from capability_kit.env import unquote as env_unquote
from capability_kit.modeltext import (
    coerce_text,
    contains_digits,
    strip_thinking,
)
from capability_kit.useragent import (
    CONTACT_ENV,
    Identity,
    UserAgentError,
    browser_user_agent,
    default_headers,
    default_user_agent,
    edgar_user_agent,
    identity_from_env,
    reddit_user_agent,
)

__all__ = [
    "BrokerClient",
    "BrokerError",
    "CONTACT_ENV",
    "coerce_text",
    "contains_digits",
    "env_getenv",
    "env_unquote",
    "strip_thinking",
    "Identity",
    "UserAgentError",
    "browser_user_agent",
    "default_headers",
    "default_user_agent",
    "edgar_user_agent",
    "identity_from_env",
    "reddit_user_agent",
    "verify_bearer",
]
