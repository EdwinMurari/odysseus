import json
import sys
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Base,
    CapabilityAdminState,
    CapabilityPreference,
    CapabilityRun,
    Document,
)
from src.capabilities import CapabilityConfigError, CapabilityRegistry
from src.capability_runner import CapabilityManager
from src.agent_tools import TOOL_TAGS
from src.task_scheduler import TaskScheduler
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from routes.capability_routes import setup_capability_routes


def _write_registry(path: Path, cwd: Path, command: list[str]) -> None:
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "capabilities:",
                "  - id: test-report",
                "    name: Test Report",
                "    description: Test capability",
                "    admin_only: false",
                "    timeout_seconds: 20",
                "    import_report: true",
                "    transport:",
                "      type: process",
                f"      cwd: {json.dumps(str(cwd))}",
                f"      command: {json.dumps(command)}",
                "      pass_env: []",
                "    inputs:",
                "      since:",
                "        type: string",
                "        default: 7d",
                "      refresh:",
                "        type: boolean",
                "        default: true",
                "        flag: --refresh",
                "        false_flag: --no-refresh",
            ]
        ),
        encoding="utf-8",
    )


def _write_http_registry(path: Path) -> None:
    path.write_text(
        """
version: 1
capabilities:
  - id: test-http
    name: Test HTTP
    admin_only: false
    timeout_seconds: 20
    transport:
      type: http
      base_url: http://worker:8080
""",
        encoding="utf-8",
    )


class _FakeAsyncClient:
    def __init__(self, *, get_payload=None, calls=None, **kwargs):
        self.get_payload = get_payload or {"status": "running"}
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return httpx.Response(
            200,
            json={"id": "provider-new", "status": "queued"},
            request=httpx.Request("POST", url),
        )

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return httpx.Response(
            200,
            json=self.get_payload,
            request=httpx.Request("GET", url),
        )

    async def delete(self, url, **kwargs):
        self.calls.append(("delete", url, kwargs))
        return httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("DELETE", url),
        )


def test_registry_validates_inputs_and_builds_argv(tmp_path):
    config = tmp_path / "capabilities.yaml"
    _write_registry(config, tmp_path, ["reporter", "run"])
    registry = CapabilityRegistry(str(config))
    definition = registry.get("test-report")

    values = definition.validate_input({})

    assert values == {"since": "7d", "refresh": True}
    assert definition.build_argv(values) == ["reporter", "run", "7d", "--refresh"]
    assert definition.build_argv(
        definition.validate_input({"refresh": False})
    ) == ["reporter", "run", "7d", "--no-refresh"]
    with pytest.raises(CapabilityConfigError, match="Unknown input"):
        definition.validate_input({"unexpected": "value"})


def test_public_definition_is_browser_safe_and_presentation_driven(tmp_path):
    config = tmp_path / "capabilities.yaml"
    config.write_text(
        f"""
version: 1
capabilities:
  - id: fixture-one
    name: Fixture One
    version: "2.4"
    implementation: fixtures.one
    admin_only: false
    presentation:
      icon: chart
      category: Analytics
      short_description: A fixture capability.
    transport:
      type: http
      base_url: http://fixture-one:8080
      token_env: FIXTURE_ONE_SECRET
    inputs:
      query:
        type: text
        label: Query
        group: Request
        placeholder: What should be analysed?
  - id: fixture-two
    name: Fixture Two
    admin_only: false
    transport:
      type: http
      base_url: http://fixture-two:8080
""",
        encoding="utf-8",
    )

    registry = CapabilityRegistry(str(config))
    public = registry.get("fixture-one").public_dict()

    assert {item.id for item in registry.list()} == {"fixture-one", "fixture-two"}
    assert public["category"] == "Analytics"
    assert public["version"] == "2.4"
    assert public["inputs"][0]["label"] == "Query"
    assert "transport" not in public
    assert "base_url" not in json.dumps(public)
    assert "token_env" not in json.dumps(public)


def test_registry_rejects_secret_run_inputs(tmp_path):
    config = tmp_path / "capabilities.yaml"
    config.write_text(
        f"""
version: 1
capabilities:
  - id: unsafe-secret
    name: Unsafe Secret
    transport:
      type: http
      base_url: http://worker:8080
    inputs:
      token:
        type: string
        secret: true
""",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityConfigError, match="cannot be secret"):
        CapabilityRegistry(str(config))


def test_registry_rejects_shell_command_strings(tmp_path):
    config = tmp_path / "capabilities.yaml"
    config.write_text(
        """
version: 1
capabilities:
  - id: unsafe-command
    name: Unsafe
    transport:
      type: process
      cwd: /tmp
      command: "python report.py && rm -rf /"
""",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityConfigError, match="string list"):
        CapabilityRegistry(str(config))


def test_public_schema_supports_every_run_input_type(tmp_path):
    config = tmp_path / "capabilities.yaml"
    inputs = "\n".join(
        f"      value_{type_name}:\n        type: {type_name}"
        for type_name in ("string", "text", "array", "integer", "number", "boolean")
    )
    config.write_text(
        "version: 1\ncapabilities:\n"
        "  - id: all-inputs\n"
        "    name: All Inputs\n"
        "    admin_only: false\n"
        "    transport:\n"
        "      type: http\n"
        "      base_url: http://worker:8080\n"
        "    inputs:\n"
        f"{inputs}\n",
        encoding="utf-8",
    )

    public = CapabilityRegistry(str(config)).get("all-inputs").public_dict()

    assert {item["type"] for item in public["inputs"]} == {
        "string", "text", "array", "integer", "number", "boolean"
    }
    assert all("secret" not in item for item in public["inputs"])


def test_capability_agent_tool_has_schema_and_parser_gate():
    names = {item["function"]["name"] for item in FUNCTION_TOOL_SCHEMAS}
    assert "manage_capabilities" in TOOL_TAGS
    assert "manage_capabilities" in names
    schema = next(
        item["function"] for item in FUNCTION_TOOL_SCHEMAS
        if item["function"]["name"] == "manage_capabilities"
    )
    actions = schema["parameters"]["properties"]["action"]["enum"]
    assert {"describe", "configure", "readiness", "explain", "rerun"} <= set(actions)


def test_task_notification_carries_capability_run_deep_link():
    scheduler = TaskScheduler(None)
    scheduler.add_notification(
        "Fixture",
        "success",
        task_id="task-1",
        owner="alice",
        capability_run_id="run-1",
    )

    notifications = scheduler.pop_notifications("alice")

    assert notifications[0]["capability_run_id"] == "run-1"


def test_existing_scheduled_tasks_table_gets_capability_columns(
    tmp_path, monkeypatch
):
    import core.database as database

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE scheduled_tasks (id VARCHAR PRIMARY KEY)"
        )
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_add_task_v2_columns()

    with engine.connect() as conn:
        columns = {
            row[1]
            for row in conn.exec_driver_sql(
                "PRAGMA table_info(scheduled_tasks)"
            )
        }
    assert {"capability_id", "capability_input"} <= columns


@pytest.mark.asyncio
async def test_process_capability_persists_run_and_imports_report(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capabilities.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)

    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    monkeypatch.setattr(runner, "_RUNS_DIR", tmp_path / "runs")

    script = (
        "from pathlib import Path; import json; "
        "Path('report.md').write_text('# Weekly report\\n\\nUseful result.'); "
        "print(json.dumps({'summary':'Report ready','report_path':'report.md'}))"
    )
    config = tmp_path / "capabilities.yaml"
    _write_registry(config, tmp_path, [sys.executable, "-c", script])
    manager = CapabilityManager(CapabilityRegistry(str(config)))
    updates = []
    await manager.start()
    try:
        created = await manager.create_run("test-report", {}, "alice")
        completed = await manager.wait(
            created["id"],
            timeout=15,
            on_update=lambda run: updates.append(run["status"]),
        )
    finally:
        await manager.stop()

    assert completed["status"] == "success"
    assert updates[-1] == "success"
    assert completed["summary"] == "Report ready"
    assert completed["document_id"]

    db = test_session()
    try:
        run = db.query(CapabilityRun).filter(CapabilityRun.id == created["id"]).one()
        document = db.query(Document).filter(Document.id == run.document_id).one()
        assert run.report_path == str((tmp_path / "report.md").resolve())
        assert document.owner == "alice"
        assert document.language == "markdown"
        assert document.is_active is True
        assert "Useful result" in document.current_content
        assert document.source_capability_id == "test-report"
        assert document.source_capability_run_id == created["id"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_process_capability_receives_durable_odysseus_run_id(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'process-run-id.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)

    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    monkeypatch.setattr(runner, "_RUNS_DIR", tmp_path / "runs")

    script = (
        "import json,os; "
        "print(json.dumps({'summary':os.environ.get('ODYSSEUS_RUN_ID','missing')}))"
    )
    config = tmp_path / "capabilities.yaml"
    _write_registry(config, tmp_path, [sys.executable, "-c", script])
    manager = CapabilityManager(CapabilityRegistry(str(config)))
    await manager.start()
    try:
        created = await manager.create_run("test-report", {}, "alice")
        completed = await manager.wait(created["id"], timeout=15)
    finally:
        await manager.stop()

    assert completed["status"] == "success"
    assert completed["summary"] == created["id"]


def test_readiness_blocks_unresolved_model_roles_and_defaults_are_owner_scoped(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'readiness.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import src.capability_runner as runner
    import src.endpoint_resolver as endpoint_resolver

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    monkeypatch.setattr(
        endpoint_resolver,
        "resolve_endpoint",
        lambda *args, **kwargs: (None, None, None),
    )
    config = tmp_path / "capabilities.yaml"
    config.write_text(
        f"""
version: 1
model_roles:
  fixture.analyse:
    setting_prefix: utility
    capabilities: [fixture-ready]
capabilities:
  - id: fixture-ready
    name: Fixture Ready
    admin_only: false
    transport:
      type: process
      cwd: {json.dumps(str(tmp_path))}
      command: [echo]
    inputs:
      query:
        type: string
        default: hello
""",
        encoding="utf-8",
    )
    manager = CapabilityManager(CapabilityRegistry(str(config)))
    definition = manager.get_definition("fixture-ready")

    readiness = asyncio.run(manager.readiness(definition, "alice"))
    saved = manager.save_defaults("fixture-ready", {"query": "alice value"}, "alice")

    assert readiness["status"] == "not_ready"
    assert readiness["checks"][0]["id"] == "model:fixture.analyse"
    assert saved == {"query": "alice value"}
    db = test_session()
    try:
        row = db.query(CapabilityPreference).one()
        assert row.owner == "alice"
        assert json.loads(row.values_json) == {"query": "alice value"}
    finally:
        db.close()


def test_admin_disable_override_does_not_remove_definition_or_history(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admin-state.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import src.capability_runner as runner

    original_session = runner.SessionLocal
    runner.SessionLocal = test_session
    try:
        config = tmp_path / "capabilities.yaml"
        _write_registry(config, tmp_path, ["reporter", "run"])
        manager = CapabilityManager(CapabilityRegistry(str(config)))
        db = test_session()
        try:
            db.add(CapabilityRun(
                id="historical-run",
                owner="alice",
                capability_id="test-report",
                transport="process",
                status="success",
                input_json="{}",
            ))
            db.commit()
        finally:
            db.close()

        disabled = manager.set_enabled("test-report", False)

        assert disabled.enabled is False
        assert manager.registry.get("test-report").enabled is True
        assert manager.get_definition("test-report").enabled is False
        assert manager.list_runs("alice")[0]["id"] == "historical-run"
        db = test_session()
        try:
            assert db.query(CapabilityAdminState).one().enabled is False
        finally:
            db.close()
    finally:
        runner.SessionLocal = original_session


def test_removed_capability_remains_in_historical_catalog(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'removed.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    config = tmp_path / "capabilities.yaml"
    config.write_text("version: 1\ncapabilities: []\n", encoding="utf-8")
    db = test_session()
    try:
        db.add(CapabilityRun(
            id="removed-run",
            owner="alice",
            capability_id="retired-engine",
            transport="http",
            status="success",
            input_json='{"query":"kept"}',
        ))
        db.commit()
    finally:
        db.close()

    manager = CapabilityManager(CapabilityRegistry(str(config)))
    views = manager.historical_definition_views("alice")

    assert [view["id"] for view in views] == ["retired-engine"]
    assert views[0]["removed"] is True
    assert views[0]["last_run"]["id"] == "removed-run"
    assert views[0]["supports_retry"] is False


def test_capability_routes_enforce_catalog_run_and_owner_policy(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'routes.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import routes.capability_routes as capability_routes
    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    monkeypatch.setattr(capability_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(
        capability_routes, "owner_is_admin_or_single_user", lambda owner: False
    )
    config = tmp_path / "capabilities.yaml"
    config.write_text(
        f"""
version: 1
capabilities:
  - id: public-engine
    name: Public Engine
    admin_only: false
    transport:
      type: process
      cwd: {json.dumps(str(tmp_path))}
      command: [echo]
  - id: admin-engine
    name: Admin Engine
    admin_only: true
    transport:
      type: process
      cwd: {json.dumps(str(tmp_path))}
      command: [echo]
""",
        encoding="utf-8",
    )
    db = test_session()
    try:
        db.add_all([
            CapabilityRun(
                id="alice-admin-run",
                owner="alice",
                capability_id="admin-engine",
                transport="process",
                status="success",
                input_json="{}",
            ),
            CapabilityRun(
                id="bob-public-run",
                owner="bob",
                capability_id="public-engine",
                transport="process",
                status="success",
                input_json="{}",
            ),
        ])
        db.commit()
    finally:
        db.close()
    app = FastAPI()
    app.include_router(
        setup_capability_routes(CapabilityManager(CapabilityRegistry(str(config))))
    )
    client = TestClient(app)

    catalog = client.get("/api/capabilities")
    assert catalog.status_code == 200
    assert [item["id"] for item in catalog.json()["capabilities"]] == [
        "public-engine"
    ]
    assert catalog.json()["can_administer"] is False
    assert client.get("/api/capabilities/admin-engine").status_code == 403
    assert client.get(
        "/api/capabilities/runs/alice-admin-run"
    ).status_code == 403
    assert client.get("/api/capabilities/runs").json()["runs"] == []


def test_invalid_registry_reload_preserves_active_definitions(tmp_path):
    config = tmp_path / "capabilities.yaml"
    _write_http_registry(config)
    registry = CapabilityRegistry(str(config))
    config.write_text(
        """
version: 1
capabilities:
  - id: broken
    name: Broken
    transport:
      type: unsupported
""",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityConfigError):
        registry.reload()

    assert registry.get("test-http").name == "Test HTTP"


@pytest.mark.asyncio
async def test_registry_reload_does_not_corrupt_active_run(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reload-active.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    monkeypatch.setattr(runner, "_RUNS_DIR", tmp_path / "runs")
    config = tmp_path / "capabilities.yaml"
    script = (
        "import json,time; time.sleep(0.2); "
        "print(json.dumps({'summary':'Pinned definition completed'}))"
    )
    _write_registry(config, tmp_path, [sys.executable, "-c", script])
    registry = CapabilityRegistry(str(config))
    manager = CapabilityManager(registry)
    await manager.start()
    try:
        created = await manager.create_run("test-report", {}, "alice")
        config.write_text("version: 1\ncapabilities: []\n", encoding="utf-8")
        registry.reload()
        completed = await manager.wait(created["id"], timeout=10)
    finally:
        await manager.stop()

    assert completed["status"] == "success"
    assert completed["summary"] == "Pinned definition completed"


def test_pain_miner_setup_never_runs_or_mutates_production_task_for_smoke():
    source = Path("scripts/configure_pain_miner_task.py").read_text(encoding="utf-8")

    assert '"/api/capabilities/pain-miner/runs"' in source
    assert "pain-miner-smoke.md" in source
    assert "pain-miner-weekly.md" in source
    assert '"sources": "hn"' in source
    assert 'f"/api/tasks/{task_id}/run"' not in source


def test_pain_miner_registry_and_compose_cover_the_full_source_contract():
    import yaml

    registry = yaml.safe_load(
        Path("config/capabilities.example.yaml").read_text(encoding="utf-8")
    )
    capability = next(
        item for item in registry["capabilities"] if item["id"] == "pain-miner"
    )
    expected_sources = {
        "hn",
        "reddit",
        "austender",
        "austender_ocds",
        "g2",
        "github",
        "appstore",
        "stackexchange",
        "bluesky",
        "rss",
    }
    configured_sources = {
        item.strip() for item in capability["inputs"]["sources"]["default"].split(",")
    }
    dependencies = {item["id"]: item for item in capability["dependencies"]}

    assert configured_sources == expected_sources
    assert dependencies["apify"]["values"] == ["g2"]
    assert dependencies["reddit"]["values"] == ["reddit"]

    compose = Path("docker-compose.capabilities.example.yml").read_text(
        encoding="utf-8"
    )
    assert ",".join(
        [
            "hn",
            "reddit",
            "austender",
            "austender_ocds",
            "g2",
            "github",
            "appstore",
            "stackexchange",
            "bluesky",
            "rss",
        ]
    ) in compose
    reddit_readiness = (
        '"reddit":{"env":["REDDIT_CLIENT_ID","REDDIT_CLIENT_SECRET",'
        '"REDDIT_USER_AGENT"]'
    )
    assert reddit_readiness in compose


def test_stock_research_setup_smokes_before_configuring_production_task():
    source = Path("scripts/configure_stock_research_task.py").read_text(
        encoding="utf-8"
    )

    assert '"/api/capabilities/stock-research/runs"' in source
    assert '"tickers": "RKLB"' in source
    assert '"capability_id": "stock-research"' in source
    assert 'f"/api/tasks/{task_id}/run"' not in source


def test_stock_research_compose_uses_broker_without_provider_secrets():
    source = Path("docker-compose.capabilities.example.yml").read_text(
        encoding="utf-8"
    )
    block = source.split("  stock-research-capability:", 1)[1].split(
        "\nvolumes:", 1
    )[0]

    assert "ODYSSEUS_MODEL_BROKER_URL=http://odysseus:7000" in block
    assert "ODYSSEUS_CAPABILITY_MODEL_TOKEN=" in block
    assert "STOCK_RESEARCH_SUMMARIZE_MODEL_ROLE=stockresearch.summarize" in block
    assert "GOOGLE_API_KEY" not in block
    assert "ANTHROPIC_API_KEY" not in block
    assert "OPENAI_API_KEY" not in block


def test_capability_ui_is_registry_driven_and_has_no_pain_miner_branch():
    source = Path("static/js/capabilities.js").read_text(encoding="utf-8")

    assert "/api/capabilities" in source
    assert "data-cap-input" in source
    assert "pain-miner" not in source
    assert "painminer" not in source
    assert 'role="dialog"' in source
    assert "capability-filter" in source
    assert "Inputs and provenance" in source
    assert "data-export-document" in source
    assert "data-open-task" in source


def test_http_progress_snapshot_is_persisted(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'progress.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    config = tmp_path / "capabilities.yaml"
    _write_http_registry(config)
    db = test_session()
    try:
        db.add(CapabilityRun(
            id="run-progress",
            owner="alice",
            capability_id="test-http",
            transport="http",
            status="running",
            input_json="{}",
        ))
        db.commit()
    finally:
        db.close()

    manager = CapabilityManager(CapabilityRegistry(str(config)))
    manager._persist_progress("run-progress", {
        "status": "running",
        "summary": "Scanning sources",
        "progress": {"completed": 4, "total": 10},
        "metrics": {"sources": 4},
    })

    run = manager.get_run("run-progress", "alice")
    assert run["summary"] == "Scanning sources"
    assert run["progress"] == {"completed": 4, "total": 10}
    assert run["metrics"] == {"sources": 4}


@pytest.mark.asyncio
async def test_http_capability_cancel_calls_provider_and_persists_terminal_state(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cancel.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    config = tmp_path / "capabilities.yaml"
    _write_http_registry(config)
    db = test_session()
    try:
        db.add(
            CapabilityRun(
                id="run-cancel",
                owner="alice",
                capability_id="test-http",
                transport="http",
                status="running",
                input_json="{}",
                provider_run_id="provider-1",
            )
        )
        db.commit()
    finally:
        db.close()
    calls = []
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(calls=calls),
    )
    manager = CapabilityManager(CapabilityRegistry(str(config)))

    assert await manager.cancel("run-cancel", "alice") is True
    assert [call[0] for call in calls] == ["delete"]
    assert calls[0][1].endswith("/v1/runs/provider-1")
    assert manager.get_run("run-cancel")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_http_capability_restart_resumes_polling_without_duplicate_start(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'resume.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    import src.capability_runner as runner

    monkeypatch.setattr(runner, "SessionLocal", test_session)
    monkeypatch.setattr(runner, "_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "_POLL_SECONDS", 0.01)
    config = tmp_path / "capabilities.yaml"
    _write_http_registry(config)
    db = test_session()
    try:
        db.add(
            CapabilityRun(
                id="run-resume",
                owner="alice",
                capability_id="test-http",
                transport="http",
                status="running",
                input_json="{}",
                provider_run_id="provider-existing",
            )
        )
        db.commit()
    finally:
        db.close()
    calls = []
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(
            calls=calls,
            get_payload={
                "status": "success",
                "result": {"summary": "Recovered report"},
            },
        ),
    )
    manager = CapabilityManager(CapabilityRegistry(str(config)))

    await manager.start()
    try:
        completed = await manager.wait("run-resume", timeout=3)
    finally:
        await manager.stop()

    assert completed["status"] == "success"
    assert completed["summary"] == "Recovered report"
    assert [call[0] for call in calls] == ["get"]
    assert calls[0][1].endswith("/v1/runs/provider-existing")
