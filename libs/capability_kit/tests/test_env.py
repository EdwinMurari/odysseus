"""capability_kit.env: dotenv-quote tolerance for compose `format: raw`."""

import pytest

from capability_kit.env import getenv, unquote


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"abc"', "abc"),
        ("'abc'", "abc"),
        ('  "abc"  ', "abc"),
        ('" spaced "', "spaced"),
        ("plain", "plain"),
        ('"', '"'),  # lone quote is content, not quoting
        ("'mismatched\"", "'mismatched\""),
        ("", ""),
        ('""', ""),
    ],
)
def test_unquote(raw, expected):
    assert unquote(raw) == expected


def test_getenv_normalizes_and_defaults(monkeypatch):
    monkeypatch.setenv("KIT_ENV_TEST", '"value"')
    assert getenv("KIT_ENV_TEST") == "value"
    monkeypatch.delenv("KIT_ENV_TEST")
    assert getenv("KIT_ENV_TEST", "fallback") == "fallback"


def test_getenv_does_not_unquote_interior_quotes(monkeypatch):
    monkeypatch.setenv("KIT_ENV_TEST", 'Name "The Bot" email')
    assert getenv("KIT_ENV_TEST") == 'Name "The Bot" email'
