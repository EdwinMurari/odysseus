"""Manifest loading and input validation for external capabilities."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from src.constants import DATA_DIR

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_MODEL_ROLE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_INPUT_TYPES = {"string", "integer", "number", "boolean", "text", "array"}
_TRANSPORTS = {"http", "process"}
_TRIGGERS = {"manual", "schedule", "event", "webhook"}


class CapabilityConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityInput:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    flag: str | None = None
    false_flag: str | None = None
    choices: tuple[Any, ...] = ()
    label: str = ""
    help: str = ""
    group: str = "General"
    advanced: bool = False
    placeholder: str = ""
    minimum: float | None = None
    maximum: float | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "label": self.label or self.name.replace("_", " ").title(),
            "description": self.description,
            "help": self.help,
            "group": self.group,
            "advanced": self.advanced,
            "placeholder": self.placeholder,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True)
class CapabilityModelRole:
    name: str
    setting_prefix: str
    capabilities: tuple[str, ...]
    enabled: bool = True
    max_prompt_chars: int = 100_000
    max_schema_bytes: int = 64_000
    max_schema_depth: int = 20
    timeout_seconds: int = 180
    max_tokens: int = 4096
    # How many corrective re-asks the broker may make when a reply fails to
    # parse/validate. Weak local models need more than one; capable hosted
    # models rarely need any. Constrained decoding handles most cases first.
    max_repair_attempts: int = 2


@dataclass(frozen=True)
class CapabilityDefinition:
    id: str
    name: str
    description: str
    transport: str
    inputs: dict[str, CapabilityInput] = field(default_factory=dict)
    admin_only: bool = True
    timeout_seconds: int = 1800
    import_report: bool = True
    command: tuple[str, ...] = ()
    cwd: str | None = None
    pass_env: tuple[str, ...] = ()
    base_url: str | None = None
    token_env: str | None = None
    start_path: str = "/v1/runs"
    status_path: str = "/v1/runs/{run_id}"
    log_path: str = "/v1/runs/{run_id}/log"
    cancel_path: str = "/v1/runs/{run_id}"
    readiness_path: str = "/v1/readiness"
    source_file: str = ""
    version: str = "1"
    implementation: str = ""
    category: str = "Other"
    icon: str = "sparkles"
    short_description: str = ""
    documentation_url: str = ""
    enabled: bool = True
    visibility: str = "catalog"
    triggers: tuple[str, ...] = ("manual", "schedule")
    model_roles: tuple[str, ...] = ()
    dependencies: tuple[dict[str, Any], ...] = ()
    output_types: tuple[str, ...] = ("report",)
    report_actions: tuple[str, ...] = ("open", "chat", "archive", "delete", "export")
    progress_phases: tuple[str, ...] = ()
    supports_cancel: bool = True
    supports_retry: bool = True
    idempotent: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "short_description": self.short_description or self.description,
            "version": self.version,
            "implementation": self.implementation,
            "category": self.category,
            "icon": self.icon,
            "documentation_url": self.documentation_url,
            "enabled": self.enabled,
            "visibility": self.visibility,
            "triggers": list(self.triggers),
            "inputs": [item.public_dict() for item in self.inputs.values()],
            "admin_only": self.admin_only,
            "timeout_seconds": self.timeout_seconds,
            "import_report": self.import_report,
            "model_roles": list(self.model_roles),
            "dependencies": [
                {
                    key: item[key]
                    for key in (
                        "id", "label", "kind", "description", "required",
                        "input", "values", "required_when_selected",
                    )
                    if key in item
                }
                for item in self.dependencies
            ],
            "output_types": list(self.output_types),
            "report_actions": list(self.report_actions),
            "progress_phases": list(self.progress_phases),
            "supports_cancel": self.supports_cancel,
            "supports_retry": self.supports_retry,
            "idempotent": self.idempotent,
        }

    def validate_input(self, raw: Any) -> dict[str, Any]:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise CapabilityConfigError("Capability input must be a JSON object")
        unknown = sorted(set(raw) - set(self.inputs))
        if unknown:
            raise CapabilityConfigError(f"Unknown input field(s): {', '.join(unknown)}")

        result: dict[str, Any] = {}
        for name, spec in self.inputs.items():
            value = raw.get(name, spec.default)
            if value is None:
                if spec.required:
                    raise CapabilityConfigError(f"Missing required input: {name}")
                continue
            value = _coerce_value(name, value, spec.type)
            if spec.choices and value not in spec.choices:
                choices = ", ".join(str(v) for v in spec.choices)
                raise CapabilityConfigError(f"{name} must be one of: {choices}")
            if isinstance(value, (int, float)):
                if spec.minimum is not None and value < spec.minimum:
                    raise CapabilityConfigError(f"{name} must be at least {spec.minimum:g}")
                if spec.maximum is not None and value > spec.maximum:
                    raise CapabilityConfigError(f"{name} must be at most {spec.maximum:g}")
            result[name] = value
        return result

    def build_argv(self, values: dict[str, Any]) -> list[str]:
        if self.transport != "process":
            raise CapabilityConfigError("Only process capabilities have argv")
        argv = list(self.command)
        for name, spec in self.inputs.items():
            if name not in values:
                continue
            value = values[name]
            if spec.type == "boolean":
                if value and spec.flag:
                    argv.append(spec.flag)
                elif not value and spec.false_flag:
                    argv.append(spec.false_flag)
                continue
            if spec.flag:
                argv.extend([
                    spec.flag,
                    ",".join(str(item) for item in value)
                    if spec.type == "array"
                    else str(value),
                ])
            else:
                argv.append(
                    ",".join(str(item) for item in value)
                    if spec.type == "array"
                    else str(value)
                )
        return argv


def _coerce_value(name: str, value: Any, type_name: str) -> Any:
    try:
        if type_name in {"string", "text"}:
            if isinstance(value, (dict, list)):
                raise TypeError
            return str(value)
        if type_name == "array":
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                return [item.strip() for item in value.split(",") if item.strip()]
            raise TypeError
        if type_name == "integer":
            if isinstance(value, bool):
                raise TypeError
            return int(value)
        if type_name == "number":
            if isinstance(value, bool):
                raise TypeError
            return float(value)
        if type_name == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in {"true", "1", "yes", "on"}:
                return True
            if isinstance(value, str) and value.lower() in {"false", "0", "no", "off"}:
                return False
            raise TypeError
    except (TypeError, ValueError):
        raise CapabilityConfigError(f"{name} must be a {type_name}") from None
    raise CapabilityConfigError(f"Unsupported input type: {type_name}")


class CapabilityRegistry:
    """Loads a single admin-controlled YAML registry."""

    def __init__(self, config_path: str | None = None):
        default = Path(DATA_DIR) / "capabilities.yaml"
        self.config_path = Path(
            config_path or os.environ.get("ODYSSEUS_CAPABILITIES_CONFIG") or default
        ).expanduser()
        self._items: dict[str, CapabilityDefinition] = {}
        self._model_roles: dict[str, CapabilityModelRole] = {}
        self.reload()

    def reload(self) -> None:
        if not self.config_path.exists():
            self._items = {}
            self._model_roles = {}
            return
        try:
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise CapabilityConfigError(
                f"Cannot read capability registry {self.config_path}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("version", 1) != 1:
            raise CapabilityConfigError("Capability registry must be a version 1 mapping")
        entries = raw.get("capabilities", [])
        if not isinstance(entries, list):
            raise CapabilityConfigError("capabilities must be a list")
        parsed: dict[str, CapabilityDefinition] = {}
        for entry in entries:
            item = self._parse_definition(entry)
            if item.id in parsed:
                raise CapabilityConfigError(f"Duplicate capability id: {item.id}")
            parsed[item.id] = item
        model_roles = self._parse_model_roles(raw.get("model_roles") or {}, parsed)
        role_names_by_capability: dict[str, list[str]] = {key: [] for key in parsed}
        for role in model_roles.values():
            for capability_id in role.capabilities:
                role_names_by_capability[capability_id].append(role.name)
        parsed = {
            key: dataclass_replace(
                item,
                model_roles=tuple(sorted(role_names_by_capability.get(key, []))),
            )
            for key, item in parsed.items()
        }
        self._items = parsed
        self._model_roles = model_roles

    def list(self) -> list[CapabilityDefinition]:
        return sorted(self._items.values(), key=lambda item: item.name.lower())

    def get(self, capability_id: str) -> CapabilityDefinition:
        item = self._items.get(capability_id)
        if not item:
            raise KeyError(capability_id)
        return item

    def get_model_role(self, role: str) -> CapabilityModelRole:
        item = self._model_roles.get(role)
        if not item:
            raise KeyError(role)
        return item

    def _parse_definition(self, raw: Any) -> CapabilityDefinition:
        if not isinstance(raw, dict):
            raise CapabilityConfigError("Each capability must be a mapping")
        capability_id = str(raw.get("id", "")).strip()
        if not _ID_RE.fullmatch(capability_id):
            raise CapabilityConfigError(f"Invalid capability id: {capability_id!r}")
        transport_raw = raw.get("transport") or {}
        if not isinstance(transport_raw, dict):
            raise CapabilityConfigError(f"{capability_id}.transport must be a mapping")
        transport = str(transport_raw.get("type", "")).strip().lower()
        if transport not in _TRANSPORTS:
            raise CapabilityConfigError(
                f"{capability_id}.transport.type must be http or process"
            )
        inputs = self._parse_inputs(capability_id, raw.get("inputs") or {})
        timeout = int(raw.get("timeout_seconds", 1800))
        if not 1 <= timeout <= 86400:
            raise CapabilityConfigError(
                f"{capability_id}.timeout_seconds must be between 1 and 86400"
            )

        kwargs: dict[str, Any] = {}
        if transport == "process":
            command = transport_raw.get("command")
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(part, str) and part for part in command)
            ):
                raise CapabilityConfigError(
                    f"{capability_id}.transport.command must be a non-empty string list"
                )
            cwd = transport_raw.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise CapabilityConfigError(
                    f"{capability_id}.transport.cwd must be an absolute path"
                )
            pass_env = transport_raw.get("pass_env") or []
            if not isinstance(pass_env, list) or not all(
                isinstance(name, str) and name for name in pass_env
            ):
                raise CapabilityConfigError(
                    f"{capability_id}.transport.pass_env must be a string list"
                )
            kwargs.update(command=tuple(command), cwd=cwd, pass_env=tuple(pass_env))
        else:
            base_url = str(transport_raw.get("base_url", "")).rstrip("/")
            parsed_url = urlparse(base_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise CapabilityConfigError(
                    f"{capability_id}.transport.base_url must be an http(s) URL"
                )
            if parsed_url.username or parsed_url.password:
                raise CapabilityConfigError(
                    f"{capability_id}.transport.base_url must not contain credentials"
                )
            kwargs.update(
                base_url=base_url,
                token_env=transport_raw.get("token_env"),
                start_path=str(transport_raw.get("start_path", "/v1/runs")),
                status_path=str(
                    transport_raw.get("status_path", "/v1/runs/{run_id}")
                ),
                log_path=str(
                    transport_raw.get("log_path", "/v1/runs/{run_id}/log")
                ),
                cancel_path=str(
                    transport_raw.get("cancel_path", "/v1/runs/{run_id}")
                ),
                readiness_path=str(
                    transport_raw.get("readiness_path", "/v1/readiness")
                ),
            )

        triggers_raw = raw.get("triggers") or ["manual", "schedule"]
        if not isinstance(triggers_raw, list) or not triggers_raw:
            raise CapabilityConfigError(f"{capability_id}.triggers must be a non-empty list")
        triggers = tuple(str(item).strip().lower() for item in triggers_raw)
        invalid_triggers = sorted(set(triggers) - _TRIGGERS)
        if invalid_triggers:
            raise CapabilityConfigError(
                f"{capability_id}.triggers contains invalid values: {', '.join(invalid_triggers)}"
            )
        dependencies_raw = raw.get("dependencies") or []
        if not isinstance(dependencies_raw, list) or not all(
            isinstance(item, dict) and item.get("id") for item in dependencies_raw
        ):
            raise CapabilityConfigError(
                f"{capability_id}.dependencies must be a list of mappings with id"
            )
        presentation = raw.get("presentation") or {}
        if not isinstance(presentation, dict):
            raise CapabilityConfigError(f"{capability_id}.presentation must be a mapping")

        return CapabilityDefinition(
            id=capability_id,
            name=str(raw.get("name") or capability_id),
            description=str(raw.get("description") or ""),
            transport=transport,
            inputs=inputs,
            admin_only=bool(raw.get("admin_only", True)),
            timeout_seconds=timeout,
            import_report=bool(raw.get("import_report", True)),
            source_file=str(self.config_path),
            version=str(raw.get("version") or "1"),
            implementation=str(raw.get("implementation") or ""),
            category=str(presentation.get("category") or raw.get("category") or "Other"),
            icon=str(presentation.get("icon") or raw.get("icon") or "sparkles"),
            short_description=str(
                presentation.get("short_description") or raw.get("short_description") or ""
            ),
            documentation_url=str(
                presentation.get("documentation_url") or raw.get("documentation_url") or ""
            ),
            enabled=bool(raw.get("enabled", True)),
            visibility=str(raw.get("visibility") or "catalog"),
            triggers=triggers,
            dependencies=tuple(dict(item) for item in dependencies_raw),
            output_types=tuple(str(item) for item in (raw.get("output_types") or ["report"])),
            report_actions=tuple(
                str(item)
                for item in (
                    raw.get("report_actions")
                    or ["open", "chat", "archive", "delete", "export"]
                )
            ),
            progress_phases=tuple(str(item) for item in (raw.get("progress_phases") or [])),
            supports_cancel=bool(raw.get("supports_cancel", True)),
            supports_retry=bool(raw.get("supports_retry", True)),
            idempotent=bool(raw.get("idempotent", False)),
            **kwargs,
        )

    @staticmethod
    def _parse_inputs(
        capability_id: str, raw: Any
    ) -> dict[str, CapabilityInput]:
        if not isinstance(raw, dict):
            raise CapabilityConfigError(f"{capability_id}.inputs must be a mapping")
        result: dict[str, CapabilityInput] = {}
        for name, spec_raw in raw.items():
            if not _ID_RE.fullmatch(str(name)):
                raise CapabilityConfigError(
                    f"Invalid input name {name!r} in {capability_id}"
                )
            if not isinstance(spec_raw, dict):
                raise CapabilityConfigError(
                    f"{capability_id}.inputs.{name} must be a mapping"
                )
            if spec_raw.get("secret"):
                raise CapabilityConfigError(
                    f"{capability_id}.inputs.{name} cannot be secret; "
                    "configure credentials through transport or dependency settings"
                )
            type_name = str(spec_raw.get("type", "string"))
            if type_name not in _INPUT_TYPES:
                raise CapabilityConfigError(
                    f"{capability_id}.inputs.{name}.type is invalid"
                )
            choices_raw = spec_raw.get("choices") or []
            if not isinstance(choices_raw, list):
                raise CapabilityConfigError(
                    f"{capability_id}.inputs.{name}.choices must be a list"
                )
            result[str(name)] = CapabilityInput(
                name=str(name),
                type=type_name,
                description=str(spec_raw.get("description") or ""),
                required=bool(spec_raw.get("required", False)),
                default=spec_raw.get("default"),
                flag=str(spec_raw["flag"]) if spec_raw.get("flag") else None,
                false_flag=(
                    str(spec_raw["false_flag"])
                    if spec_raw.get("false_flag")
                    else None
                ),
                choices=tuple(choices_raw),
                label=str(spec_raw.get("label") or ""),
                help=str(spec_raw.get("help") or ""),
                group=str(spec_raw.get("group") or "General"),
                advanced=bool(spec_raw.get("advanced", False)),
                placeholder=str(spec_raw.get("placeholder") or ""),
                minimum=(
                    float(spec_raw["minimum"])
                    if spec_raw.get("minimum") is not None
                    else None
                ),
                maximum=(
                    float(spec_raw["maximum"])
                    if spec_raw.get("maximum") is not None
                    else None
                ),
            )
        return result

    @staticmethod
    def _parse_model_roles(
        raw: Any, capabilities: dict[str, CapabilityDefinition]
    ) -> dict[str, CapabilityModelRole]:
        if not isinstance(raw, dict):
            raise CapabilityConfigError("model_roles must be a mapping")
        result: dict[str, CapabilityModelRole] = {}
        for name, spec in raw.items():
            role = str(name).strip()
            if not _MODEL_ROLE_RE.fullmatch(role):
                raise CapabilityConfigError(f"Invalid model role: {role!r}")
            if not isinstance(spec, dict):
                raise CapabilityConfigError(f"model_roles.{role} must be a mapping")
            setting_prefix = str(spec.get("setting_prefix", "")).strip()
            if not _ID_RE.fullmatch(setting_prefix):
                raise CapabilityConfigError(
                    f"model_roles.{role}.setting_prefix is invalid"
                )
            allowed = spec.get("capabilities") or []
            if not isinstance(allowed, list) or not allowed or not all(
                isinstance(value, str) and value in capabilities for value in allowed
            ):
                raise CapabilityConfigError(
                    f"model_roles.{role}.capabilities must list registered capabilities"
                )

            limits = {
                "max_prompt_chars": (1, 1_000_000, 100_000),
                "max_schema_bytes": (128, 1_000_000, 64_000),
                "max_schema_depth": (1, 100, 20),
                "timeout_seconds": (1, 3600, 180),
                "max_tokens": (1, 131_072, 4096),
                "max_repair_attempts": (0, 5, 2),
            }
            values: dict[str, int] = {}
            for field_name, (minimum, maximum, default) in limits.items():
                try:
                    value = int(spec.get(field_name, default))
                except (TypeError, ValueError):
                    raise CapabilityConfigError(
                        f"model_roles.{role}.{field_name} must be an integer"
                    ) from None
                if not minimum <= value <= maximum:
                    raise CapabilityConfigError(
                        f"model_roles.{role}.{field_name} must be between "
                        f"{minimum} and {maximum}"
                    )
                values[field_name] = value

            result[role] = CapabilityModelRole(
                name=role,
                setting_prefix=setting_prefix,
                capabilities=tuple(allowed),
                enabled=bool(spec.get("enabled", True)),
                **values,
            )
        return result


def dumps_input(values: dict[str, Any]) -> str:
    return json.dumps(values, separators=(",", ":"), sort_keys=True)


def dataclass_replace(value, **changes):
    from dataclasses import replace

    return replace(value, **changes)
