"""Environment-variable reading that tolerates dotenv-style quoting.

The same ``.env`` file reaches capability code through two different parsers:

- locally, ``python-dotenv`` loads it and strips surrounding quotes
  (standard dotenv semantics);
- in the worker container, docker compose injects it via
  ``env_file: format: raw``, which passes every line through verbatim —
  quotes included. ``raw`` is deliberate (it keeps ``$`` in secrets from
  being interpolated), so the quotes must be tolerated here instead.

Without this normalization a value written as ``FRED_API_KEY="abc"`` works
on a laptop and silently breaks in the container, where the literal
``"abc"`` (quotes and all) is sent to the provider and rejected. Every
capability config must read env values through :func:`getenv` (or pass
individual values through :func:`unquote`) so both paths behave identically.
"""

from __future__ import annotations

import os

__all__ = ["getenv", "unquote"]


def unquote(value: str) -> str:
    """Strip whitespace and one pair of matching surrounding quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def getenv(key: str, default: str = "") -> str:
    """``os.getenv`` with dotenv-equivalent quote handling."""
    raw = os.getenv(key)
    if raw is None:
        return default
    return unquote(raw)
