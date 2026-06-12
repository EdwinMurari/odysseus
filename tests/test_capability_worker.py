import pytest

from integrations.capabilities import worker


def test_worker_emits_explicit_false_boolean_flag(monkeypatch):
    monkeypatch.setattr(worker, "COMMAND", ["painminer", "run"])
    monkeypatch.setattr(
        worker,
        "INPUTS",
        {
            "refresh": {
                "type": "boolean",
                "default": True,
                "flag": "--refresh",
                "false_flag": "--no-refresh",
            }
        },
    )

    assert worker._argv({"refresh": True}) == [
        "painminer",
        "run",
        "--refresh",
    ]
    assert worker._argv({"refresh": False}) == [
        "painminer",
        "run",
        "--no-refresh",
    ]


@pytest.mark.asyncio
async def test_worker_readiness_reports_status_without_returning_secrets(
    monkeypatch,
):
    monkeypatch.setattr(worker, "TOKEN", "")
    monkeypatch.setattr(
        worker,
        "READINESS",
        {
            "source-api": {
                "env": ["SOURCE_API_TOKEN"],
                "missing_message": "Source API is unavailable",
            }
        },
    )
    monkeypatch.setenv("SOURCE_API_TOKEN", "super-secret")

    result = await worker.readiness()

    assert result == {
        "ready": True,
        "dependencies": [
            {
                "id": "source-api",
                "available": True,
                "detail": "Available",
            }
        ],
    }
    assert "super-secret" not in str(result)


@pytest.mark.asyncio
async def test_worker_readiness_tracks_source_credentials_independently(
    monkeypatch,
):
    monkeypatch.setattr(worker, "TOKEN", "")
    monkeypatch.setattr(
        worker,
        "READINESS",
        {
            "apify": {
                "env": ["APIFY_TOKEN"],
                "missing_message": "G2 collection is unavailable.",
            },
            "reddit": {
                "env": [
                    "REDDIT_CLIENT_ID",
                    "REDDIT_CLIENT_SECRET",
                    "REDDIT_USER_AGENT",
                ],
                "missing_message": "Reddit collection is unavailable.",
            },
        },
    )
    monkeypatch.setenv("APIFY_TOKEN", "configured")
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)

    result = await worker.readiness()

    assert result == {
        "ready": False,
        "dependencies": [
            {"id": "apify", "available": True, "detail": "Available"},
            {
                "id": "reddit",
                "available": False,
                "detail": "Reddit collection is unavailable.",
            },
        ],
    }
