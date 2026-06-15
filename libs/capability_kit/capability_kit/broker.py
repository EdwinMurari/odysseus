"""Client for the Odysseus capability-model broker (ADR-025 contract).

Workers never hold provider credentials. They POST a prompt + JSON Schema to
``/api/capability-models/structured`` with a scoped bearer token and an
active run id; Odysseus resolves the model role through its own settings,
validates the response against the schema, and returns
``{"data": ..., "provenance": {...}}``.

This client was previously duplicated near-verbatim in pain-miner and
stock-research. It is now the single implementation.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, TypeVar

import httpx
from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)

_PROVENANCE_KEYS = ("role", "setting_prefix", "model", "endpoint")

BROKER_URL_ENV = "ODYSSEUS_MODEL_BROKER_URL"
BROKER_TOKEN_ENV = "ODYSSEUS_CAPABILITY_MODEL_TOKEN"


class BrokerError(RuntimeError):
    """The broker rejected the call or returned an invalid response."""


class BrokerClient:
    """Structured model calls via Odysseus-owned endpoints and credentials."""

    def __init__(
        self,
        base_url: str,
        token: str,
        run_id: str,
        role: str,
        *,
        timeout_seconds: float = 180,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.run_id = run_id
        self.role = role
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.user_agent = user_agent
        self._provenance: list[dict[str, str]] = []

    @classmethod
    def from_environment(
        cls,
        run_id: str,
        role: str,
        *,
        url_env: str = BROKER_URL_ENV,
        token_env: str = BROKER_TOKEN_ENV,
        **kwargs: Any,
    ) -> "BrokerClient | None":
        """Build a client from the worker environment; None when not wired."""
        base_url = os.getenv(url_env, "").strip()
        token = os.getenv(token_env, "").strip()
        if not (base_url and token and run_id and role):
            return None
        return cls(base_url, token, run_id, role, **kwargs)

    def structured_call(self, prompt: str, schema: Mapping[str, Any]) -> Any:
        """POST one structured call; returns the schema-validated ``data``."""
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(
                f"{self.base_url}/api/capability-models/structured",
                headers=headers,
                json={
                    "run_id": self.run_id,
                    "role": self.role,
                    "prompt": prompt,
                    "schema": dict(schema),
                },
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = response.json().get("detail")
            except (TypeError, ValueError, AttributeError):
                detail = None
            raise BrokerError(
                f"Odysseus model broker failed ({response.status_code}): "
                f"{detail or response.text}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict) or "data" not in payload:
            raise BrokerError("Odysseus model broker returned an invalid response")
        self._record_provenance(payload.get("provenance"))
        return payload["data"]

    def structured_call_model(self, prompt: str, model_cls: type[ModelT]) -> ModelT:
        """Structured call with a pydantic model as both schema and parser."""
        data = self.structured_call(prompt, model_cls.model_json_schema())
        return model_cls.model_validate(data)

    def narrative_text(
        self, prompt: str, *, key: str = "narrative", max_chars: int = 4000
    ) -> str:
        """Clean prose from a weak model — the shared discipline for any
        capability that lets a local model write report narrative.

        Asks for a one-field JSON object (the easiest shape for a weak model)
        but does NOT trust it: the reply is thinking-stripped and tolerantly
        coerced to text (``modeltext``), whether the model returned JSON, a
        bare string, or JSON-wrapped-in-prose. Returns the cleaned, length-
        capped prose (possibly ``""``). Does not validate content — callers
        apply their own rules (e.g. ``contains_digits`` to reject invented
        figures) and decide on fallback. Raises only on broker transport
        failure, like ``structured_call``.
        """
        from capability_kit.modeltext import coerce_text, strip_thinking

        schema = {
            "type": "object",
            "properties": {key: {"type": "string", "maxLength": max_chars}},
            "required": [key],
            "additionalProperties": False,
        }
        data = self.structured_call(prompt, schema)
        return strip_thinking(coerce_text(data, key))[:max_chars].strip()

    def provenance(self) -> list[dict[str, str]]:
        """Distinct (role, setting_prefix, model, endpoint) records seen so far."""
        return [dict(item) for item in self._provenance]

    def _record_provenance(self, provenance: Any) -> None:
        if not isinstance(provenance, dict):
            return
        normalized = {
            key: str(value)
            for key, value in provenance.items()
            if key in _PROVENANCE_KEYS and value is not None
        }
        if normalized and normalized not in self._provenance:
            self._provenance.append(normalized)
