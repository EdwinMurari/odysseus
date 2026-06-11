"""Authenticated structured-model broker for capability workers."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.capability_model_broker import (
    CapabilityModelBrokerError,
    authorize_broker_call,
    invoke_structured_model,
)


class StructuredModelRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=128)
    prompt: str
    schema_: dict[str, Any] = Field(alias="schema")


def setup_capability_model_routes(manager, token: str) -> APIRouter:
    router = APIRouter(prefix="/api/capability-models", tags=["capabilities"])

    @router.post("/structured")
    async def structured_call(
        body: StructuredModelRequest,
        authorization: str | None = Header(default=None),
    ):
        if not token or not authorization or not secrets.compare_digest(
            authorization, f"Bearer {token}"
        ):
            raise HTTPException(401, "Invalid capability model token")
        try:
            context = authorize_broker_call(
                manager.registry, body.run_id, body.role
            )
            return await invoke_structured_model(
                context, body.prompt, body.schema_
            )
        except CapabilityModelBrokerError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None

    return router
