"""HTTP API for registered capabilities and their durable runs."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth_helpers import get_current_user
from src.capabilities import CapabilityConfigError
from src.tool_security import owner_is_admin_or_single_user


class CapabilityRunCreate(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class CapabilityDefaultsUpdate(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class CapabilityEnabledUpdate(BaseModel):
    enabled: bool


def setup_capability_routes(manager) -> APIRouter:
    router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])

    def _owner(request: Request) -> str | None:
        return get_current_user(request)

    def _is_admin(owner: str | None) -> bool:
        if owner == "internal-tool":
            return True
        return owner_is_admin_or_single_user(owner)

    def _authorize_definition(owner: str | None, capability_id: str):
        try:
            definition = manager.get_definition(capability_id)
        except KeyError:
            raise HTTPException(404, "Capability not found") from None
        if definition.admin_only and not _is_admin(owner):
            raise HTTPException(403, "This capability requires an admin user")
        return definition

    def _authorize_run(owner: str | None, run_id: str):
        run = manager.get_run(run_id, owner)
        if not run:
            raise HTTPException(404, "Capability run not found")
        try:
            definition = manager.get_definition(run["capability_id"])
        except KeyError:
            definition = None
        if definition and definition.admin_only and not _is_admin(owner):
            raise HTTPException(403, "This capability requires an admin user")
        return run

    @router.get("")
    async def list_capabilities(request: Request):
        owner = _owner(request)
        items = []
        for item in manager.registry.list():
            if item.admin_only and not _is_admin(owner):
                continue
            if item.visibility == "hidden":
                continue
            items.append(item)
        views = await asyncio.gather(
            *(manager.definition_view(item, owner) for item in items)
        )
        for view in views:
            view["can_administer"] = _is_admin(owner)
        historical = manager.historical_definition_views(owner)
        for view in historical:
            view["can_administer"] = False
        views.extend(historical)
        return {
            "capabilities": views,
            "can_administer": _is_admin(owner),
        }

    @router.put("/{capability_id}/defaults")
    async def update_defaults(
        request: Request, capability_id: str, body: CapabilityDefaultsUpdate
    ):
        owner = _owner(request)
        _authorize_definition(owner, capability_id)
        try:
            values = manager.save_defaults(capability_id, body.input, owner)
        except CapabilityConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, "saved_defaults": values}

    @router.post("/{capability_id}/readiness")
    async def check_readiness(
        request: Request, capability_id: str, body: CapabilityRunCreate
    ):
        owner = _owner(request)
        definition = _authorize_definition(owner, capability_id)
        try:
            values = definition.validate_input(body.input)
        except CapabilityConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        return await manager.readiness(definition, owner, values)

    @router.put("/{capability_id}/enabled")
    async def update_enabled(
        request: Request, capability_id: str, body: CapabilityEnabledUpdate
    ):
        owner = _owner(request)
        if not _is_admin(owner):
            raise HTTPException(403, "Admin access required")
        try:
            definition = manager.set_enabled(capability_id, body.enabled)
        except KeyError:
            raise HTTPException(404, "Capability not found") from None
        view = await manager.definition_view(definition, owner)
        view["can_administer"] = True
        return view

    @router.post("/reload")
    async def reload_capabilities(request: Request):
        owner = _owner(request)
        if not _is_admin(owner):
            raise HTTPException(403, "Admin access required")
        try:
            manager.registry.reload()
        except CapabilityConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, "capabilities": manager.list_definitions()}

    @router.get("/runs")
    async def list_runs(
        request: Request, capability_id: str | None = None, limit: int = 50
    ):
        owner = _owner(request)
        if capability_id:
            try:
                _authorize_definition(owner, capability_id)
            except HTTPException as exc:
                if exc.status_code != 404 or not manager.historical_definition_view(
                    capability_id, owner
                ):
                    raise
        runs = manager.list_runs(owner, capability_id=capability_id, limit=limit)
        visible = []
        for run in runs:
            try:
                definition = manager.get_definition(run["capability_id"])
            except KeyError:
                definition = None
            if definition and definition.admin_only and not _is_admin(owner):
                continue
            visible.append(run)
        return {"runs": visible}

    @router.post("/{capability_id}/runs")
    async def create_run(
        request: Request, capability_id: str, body: CapabilityRunCreate
    ):
        owner = _owner(request)
        _authorize_definition(owner, capability_id)
        try:
            return await manager.create_run(capability_id, body.input, owner)
        except CapabilityConfigError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/runs/{run_id}")
    async def get_run(request: Request, run_id: str):
        return _authorize_run(_owner(request), run_id)

    @router.get("/runs/{run_id}/log")
    async def get_run_log(request: Request, run_id: str, tail: int = 400):
        owner = _owner(request)
        _authorize_run(owner, run_id)
        output = await manager.get_log(run_id, owner, tail)
        if output is None:
            raise HTTPException(404, "Capability run not found")
        return {"run_id": run_id, "output": output}

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str):
        owner = _owner(request)
        _authorize_run(owner, run_id)
        if not await manager.cancel(run_id, owner):
            raise HTTPException(409, "Capability run is not active")
        return {"ok": True}

    @router.post("/runs/{run_id}/rerun")
    async def rerun(request: Request, run_id: str):
        owner = _owner(request)
        previous = manager.get_run(run_id, owner)
        if not previous:
            raise HTTPException(404, "Capability run not found")
        definition = _authorize_definition(owner, previous["capability_id"])
        if not definition.supports_retry:
            raise HTTPException(409, "This capability does not support reruns")
        try:
            return await manager.create_run(
                definition.id, previous.get("input") or {}, owner
            )
        except CapabilityConfigError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/{capability_id}")
    async def get_capability(request: Request, capability_id: str):
        owner = _owner(request)
        try:
            definition = _authorize_definition(owner, capability_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            historical = manager.historical_definition_view(capability_id, owner)
            if not historical:
                raise
            historical["can_administer"] = False
            return historical
        view = await manager.definition_view(definition, owner)
        view["can_administer"] = _is_admin(owner)
        return view

    return router
