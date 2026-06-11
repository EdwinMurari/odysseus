"""Run-scoped structured model invocation for trusted capability workers."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from core.database import CapabilityRun, SessionLocal
from src.capabilities import CapabilityModelRole, CapabilityRegistry
from src.endpoint_resolver import resolve_endpoint
from src.llm_core import llm_call_async

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_ACTIVE_STATUSES = {"queued", "running"}


class CapabilityModelBrokerError(RuntimeError):
    status_code = 502


class CapabilityModelAuthorizationError(CapabilityModelBrokerError):
    status_code = 403


class CapabilityModelRequestError(CapabilityModelBrokerError):
    status_code = 400


class CapabilityModelUnavailableError(CapabilityModelBrokerError):
    status_code = 503


@dataclass(frozen=True)
class BrokerContext:
    run_id: str
    owner: str | None
    capability_id: str
    role: CapabilityModelRole


def _schema_depth(value: Any, depth: int = 0) -> int:
    if isinstance(value, dict):
        return max([depth] + [_schema_depth(item, depth + 1) for item in value.values()])
    if isinstance(value, list):
        return max([depth] + [_schema_depth(item, depth + 1) for item in value])
    return depth


def authorize_broker_call(
    registry: CapabilityRegistry, run_id: str, role_name: str
) -> BrokerContext:
    try:
        role = registry.get_model_role(role_name)
    except KeyError:
        raise CapabilityModelAuthorizationError("Unknown model role") from None
    if not role.enabled:
        raise CapabilityModelAuthorizationError("Model role is disabled")

    db = SessionLocal()
    try:
        run = db.query(CapabilityRun).filter(CapabilityRun.id == run_id).first()
        if not run or run.status not in _ACTIVE_STATUSES:
            raise CapabilityModelAuthorizationError("Capability run is not active")
        if run.capability_id not in role.capabilities:
            raise CapabilityModelAuthorizationError(
                "Capability is not allowed to use this model role"
            )
        return BrokerContext(
            run_id=run.id,
            owner=run.owner,
            capability_id=run.capability_id,
            role=role,
        )
    finally:
        db.close()


def _run_is_active(run_id: str) -> bool:
    db = SessionLocal()
    try:
        run = db.query(CapabilityRun.status).filter(CapabilityRun.id == run_id).first()
        return bool(run and run[0] in _ACTIVE_STATUSES)
    finally:
        db.close()


async def _call_while_run_active(context: BrokerContext, **kwargs) -> str:
    call = asyncio.create_task(llm_call_async(**kwargs))
    try:
        while not call.done():
            await asyncio.sleep(0.25)
            if not await asyncio.to_thread(_run_is_active, context.run_id):
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                raise CapabilityModelAuthorizationError(
                    "Capability run was cancelled during model invocation"
                )
        return await call
    finally:
        if not call.done():
            call.cancel()


def validate_broker_request(
    context: BrokerContext, prompt: str, schema: dict[str, Any]
) -> None:
    if not prompt or len(prompt) > context.role.max_prompt_chars:
        raise CapabilityModelRequestError(
            f"prompt must contain 1-{context.role.max_prompt_chars} characters"
        )
    if not isinstance(schema, dict):
        raise CapabilityModelRequestError("schema must be a JSON object")
    encoded = json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > context.role.max_schema_bytes:
        raise CapabilityModelRequestError("schema exceeds the configured size limit")
    if _schema_depth(schema) > context.role.max_schema_depth:
        raise CapabilityModelRequestError("schema exceeds the configured depth limit")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise CapabilityModelRequestError(f"invalid JSON schema: {exc.message}") from None


def _parse_json_response(text: str) -> Any:
    match = _JSON_FENCE_RE.match(text)
    if match:
        text = match.group(1)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                pass
    raise CapabilityModelBrokerError("Model returned invalid JSON")


async def invoke_structured_model(
    context: BrokerContext, prompt: str, schema: dict[str, Any]
) -> dict[str, Any]:
    validate_broker_request(context, prompt, schema)
    url, model, headers = resolve_endpoint(
        context.role.setting_prefix, owner=context.owner
    )
    if not url or not model:
        raise CapabilityModelUnavailableError(
            f"No endpoint/model configured for role {context.role.name}"
        )

    schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    messages = [
        {
            "role": "system",
            "content": (
                "Return only one JSON value that validates against the supplied "
                "JSON Schema. Do not use markdown fences or add commentary.\n\n"
                f"JSON Schema:\n{schema_json}"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        raw = await _call_while_run_active(
            context,
            url=url,
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=context.role.max_tokens,
            headers=headers,
            timeout=context.role.timeout_seconds,
            max_retries=1,
            prompt_type=f"capability:{context.role.name}",
            session_id=context.run_id,
        )
    except CapabilityModelAuthorizationError:
        raise
    except Exception as exc:
        raise CapabilityModelBrokerError(
            f"Model invocation failed: {type(exc).__name__}: {exc}"
        ) from exc

    value = _parse_json_response(raw)
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise CapabilityModelBrokerError(
            f"Model response failed schema validation: {exc.message}"
        ) from None
    return {
        "data": value,
        "provenance": {
            "role": context.role.name,
            "setting_prefix": context.role.setting_prefix,
            "model": model,
            "endpoint": url,
        },
    }
