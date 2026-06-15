"""Run-scoped structured model invocation for trusted capability workers.

Structured output is enforced primarily via constrained decoding (the schema is
passed to the model server), with tolerant parsing and bounded repair re-asks as
fallbacks so weak local models still return schema-valid JSON.
"""

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
# Small reasoning-tuned local models (gemma, qwen, deepseek-distill, ...) often
# wrap their answer in a chain-of-thought block before the JSON. Strip any such
# block so tolerant extraction sees only the answer.
_THINK_BLOCK_RE = re.compile(
    r"<(think|thinking|reasoning|reflection|scratchpad)>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
# Trailing commas before a closing brace/bracket are the single most common
# way a weak model produces "almost-JSON". JSON forbids them; we strip them as
# a last-resort repair before giving up on a reply.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
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


def _balanced_json_slice(text: str) -> str | None:
    """Return the first balanced ``{...}`` or ``[...]`` span in ``text``.

    Scans with string/escape awareness so braces inside string literals don't
    throw off the depth count. Weak models routinely wrap the JSON in prose
    ("Here is the JSON: {...}. Hope that helps!"); this recovers the value
    without depending on it being the only thing in the reply.
    """
    start = None
    opener = None
    closer = None
    depth = 0
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if start is None:
            if ch in "{[":
                start = i
                opener = ch
                closer = "}" if ch == "{" else "]"
                depth = 1
            continue
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _loads_lenient(candidate: str) -> Any:
    """``json.loads`` with one tolerant retry that drops trailing commas."""
    try:
        return json.loads(candidate)
    except (TypeError, ValueError):
        repaired = _TRAILING_COMMA_RE.sub(r"\1", candidate)
        return json.loads(repaired)


def _parse_json_response(text: str) -> Any:
    """Extract one JSON value from a raw model reply, tolerantly.

    Order of attempts, cheapest/most-exact first:
      1. strip reasoning blocks the model may have emitted;
      2. parse the whole (trimmed) reply, then a ```fenced``` body;
      3. fall back to the first balanced ``{...}``/``[...]`` span in the text.
    Each candidate is parsed with lenient (trailing-comma-tolerant) loading.
    """
    cleaned = _THINK_BLOCK_RE.sub("", text).strip()

    candidates: list[str] = []
    if cleaned:
        candidates.append(cleaned)
    fence = _JSON_FENCE_RE.match(cleaned)
    if fence:
        candidates.append(fence.group(1).strip())
    sliced = _balanced_json_slice(cleaned)
    if sliced:
        candidates.append(sliced)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return _loads_lenient(candidate)
        except (TypeError, ValueError):
            continue
    raise CapabilityModelBrokerError("Model returned invalid JSON")


def _parse_and_validate(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    """Parse the raw model reply and validate it against the schema.

    Returns ``(value, None)`` on success or ``(None, error_message)`` when the
    reply is unusable. The error message is suitable both for the eventual
    broker error and for feeding back to the model in a repair attempt.
    """
    try:
        value = _parse_json_response(text)
    except CapabilityModelBrokerError as exc:
        return None, str(exc)
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        return None, f"Model response failed schema validation: {exc.message}"
    return value, None


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

    async def _invoke(
        call_messages: list[dict[str, str]], *, constrain: bool
    ) -> str:
        try:
            return await _call_while_run_active(
                context,
                url=url,
                model=model,
                messages=call_messages,
                temperature=0,
                max_tokens=context.role.max_tokens,
                headers=headers,
                timeout=context.role.timeout_seconds,
                max_retries=1,
                prompt_type=f"capability:{context.role.name}",
                session_id=context.run_id,
                # Constrained decoding is the primary defence against weak local
                # models: the server is handed the JSON Schema and forced onto
                # the grammar, so the reply is valid JSON by construction.
                # Endpoints that don't support it ignore the field; the tolerant
                # parser and repair loop below are the fallback for those.
                response_format=schema if constrain else None,
            )
        except CapabilityModelAuthorizationError:
            raise
        except Exception as exc:
            raise CapabilityModelBrokerError(
                f"Model invocation failed: {type(exc).__name__}: {exc}"
            ) from exc

    try:
        raw = await _invoke(messages, constrain=True)
    except CapabilityModelBrokerError:
        # The endpoint rejected the request outright — most likely it doesn't
        # accept the response_format field. Retry once without constrained
        # decoding so constraint-incompatible endpoints still work (they then
        # rely on the tolerant parser + repair loop). A second failure here is
        # a real outage and propagates.
        raw = await _invoke(messages, constrain=False)
    value, error = _parse_and_validate(raw, schema)

    # Corrective rounds before failing. Small utility models routinely miss a
    # required key or wrap the value on the first try; feeding the validator
    # message back recovers the majority without weakening the schema contract.
    # A reply that still doesn't validate after the configured attempts is
    # rejected exactly as before.
    attempt = 0
    while error is not None and attempt < context.role.max_repair_attempts:
        attempt += 1
        repair_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your previous reply was rejected: {error}. "
                    "Reply again with exactly one JSON value that validates "
                    "against the JSON Schema in the system message. Output "
                    "only the corrected JSON — no fences, no commentary."
                ),
            },
        ]
        raw = await _invoke(repair_messages, constrain=True)
        value, error = _parse_and_validate(raw, schema)

    if error is not None:
        raise CapabilityModelBrokerError(error)
    return {
        "data": value,
        "provenance": {
            "role": context.role.name,
            "setting_prefix": context.role.setting_prefix,
            "model": model,
            "endpoint": url,
        },
    }
