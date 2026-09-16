"""Shared pytest fixtures for pg-sessions tests."""

from __future__ import annotations

import os
from typing import Any

import pytest


class FakeCtx:
    """Minimal stand-in for Hermes' PluginContext.

    Records every registration so tests can assert on what register() wired
    up without a running Hermes process.
    """

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.hooks: list[dict[str, Any]] = []
        self.cli: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_hook(self, name: str, handler: Any) -> None:
        self.hooks.append({"name": name, "handler": handler})

    def register_cli_command(self, **kwargs: Any) -> None:
        self.cli.append(kwargs)

    def register_skill(self, name: str, path: str, description: str = "") -> None:
        self.skills.append({"name": name, "path": path, "description": description})


@pytest.fixture
def fake_ctx() -> FakeCtx:
    return FakeCtx()


@pytest.fixture
def no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure PG_SESSIONS_URL is unset for this test."""
    monkeypatch.delenv("PG_SESSIONS_URL", raising=False)


@pytest.fixture
def test_dsn() -> str:
    """Integration-test DSN, or skip the test when unset."""
    dsn = os.environ.get("PG_SESSIONS_URL_TEST", "")
    if not dsn:
        pytest.skip("PG_SESSIONS_URL_TEST not set — skipping integration test")
    return dsn
