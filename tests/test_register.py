"""Registration tests — register(ctx) wires exactly the documented surface."""

from __future__ import annotations

import inspect

import pg_sessions

EXPECTED_TOOLS = {"sessions_query", "sessions_get", "sessions_stats", "sessions_migrate"}
EXPECTED_HOOKS = {"on_session_start", "post_llm_call", "on_session_end", "on_session_reset"}


def test_register_wires_all_tools(fake_ctx, no_dsn) -> None:
    pg_sessions.register(fake_ctx)

    names = {t["name"] for t in fake_ctx.tools}
    assert names == EXPECTED_TOOLS


def test_tools_use_dedicated_toolset(fake_ctx, no_dsn) -> None:
    pg_sessions.register(fake_ctx)

    toolsets = {t["toolset"] for t in fake_ctx.tools}
    assert toolsets == {"pg_sessions"}, "must not register under a core toolset"


def test_tool_handlers_and_check_fns_callable(fake_ctx, no_dsn) -> None:
    pg_sessions.register(fake_ctx)

    for tool in fake_ctx.tools:
        assert callable(tool["handler"]), tool["name"]
        assert callable(tool["check_fn"]), tool["name"]
        assert tool["schema"]["name"] == tool["name"]


def test_register_wires_all_hooks(fake_ctx, no_dsn) -> None:
    pg_sessions.register(fake_ctx)

    names = {h["name"] for h in fake_ctx.hooks}
    assert names == EXPECTED_HOOKS


def test_hooks_accept_session_id(fake_ctx, no_dsn) -> None:
    """Hook payloads are keyword payloads — every callback takes session_id."""
    pg_sessions.register(fake_ctx)

    for hook in fake_ctx.hooks:
        params = inspect.signature(hook["handler"]).parameters
        assert "session_id" in params, hook["name"]


def test_hooks_tolerate_unknown_kwargs(fake_ctx, no_dsn) -> None:
    """Forward compatibility: callbacks must accept **kwargs."""
    pg_sessions.register(fake_ctx)

    for hook in fake_ctx.hooks:
        params = inspect.signature(hook["handler"]).parameters
        has_var_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
        assert has_var_kw, f"{hook['name']} must accept **kwargs"


def test_register_wires_cli_command(fake_ctx, no_dsn) -> None:
    pg_sessions.register(fake_ctx)

    assert len(fake_ctx.cli) == 1
    cmd = fake_ctx.cli[0]
    assert cmd["name"] == "pg-sessions"
    assert callable(cmd["setup_fn"])
    assert callable(cmd["handler_fn"])


def test_check_fn_hides_tools_without_dsn(fake_ctx, no_dsn) -> None:
    """No DSN → tools are registered but invisible to the model."""
    result = pg_sessions._check_available()
    assert isinstance(result, tuple)
    ok, message = result
    assert ok is False
    assert "PG_SESSIONS_URL" in message


def test_check_fn_shows_tools_with_dsn(monkeypatch) -> None:
    monkeypatch.setenv("PG_SESSIONS_URL", "postgresql://u:p@localhost:5432/db")
    assert pg_sessions._check_available() is True


def test_get_store_returns_none_without_dsn(no_dsn, monkeypatch) -> None:
    monkeypatch.setattr(pg_sessions, "_store", None)
    assert pg_sessions._get_store() is None
