"""pg-sessions — PostgreSQL session storage for Hermes Agent.

Stores session history and conversation turns in PostgreSQL instead of (or
alongside) the local SQLite ``state.db``.

Compatible with Neon, Supabase, Aiven, DigitalOcean, RDS, and any standard
PostgreSQL 12+ instance.

Configuration — set ``PG_SESSIONS_URL`` in ``$HERMES_HOME/.env``:

    # Neon
    PG_SESSIONS_URL=postgresql://user:pass@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require

    # Neon (pooled — recommended for serverless)
    PG_SESSIONS_URL=postgresql://user:pass@ep-xxxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require

    # Supabase (transaction pooler, port 6543)
    PG_SESSIONS_URL=postgresql://postgres:pass@db.xxxx.supabase.co:6543/postgres?sslmode=require

    # Standard
    PG_SESSIONS_URL=postgresql://user:pass@host:5432/dbname

Install::

    pip install hermes-pg-sessions

Then enable the plugin::

    hermes plugins enable pg-sessions

CLI::

    hermes pg-sessions status
    hermes pg-sessions stats --days 30
    hermes pg-sessions migrate --source ~/.hermes/state.db --dry-run
    hermes pg-sessions connect --dsn "postgresql://..."

Tools exposed to the model: ``sessions_query``, ``sessions_get``,
``sessions_stats``, ``sessions_migrate``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional

from . import schemas, tools
from .store import PGSessionStore

__version__ = "1.0.0"
__all__ = ["register", "PGSessionStore", "__version__"]

logger = logging.getLogger("plugins.pg-sessions")

# Module-level singleton — shared across register() and all hook callbacks.
_store: Optional[PGSessionStore] = None
_store_lock = threading.Lock()


def _get_store() -> Optional[PGSessionStore]:
    """Return the connected store singleton, or None when unconfigured."""
    global _store
    if _store is not None:
        return _store

    dsn = os.environ.get("PG_SESSIONS_URL", "")
    if not dsn:
        return None

    with _store_lock:
        if _store is not None:
            return _store
        try:
            store = PGSessionStore()
            store.connect(dsn)
            _store = store
            logger.info(
                "pg-sessions: connected (provider=%s, pooled=%s)",
                store._provider,
                store._is_pooled,
            )
        except Exception as exc:  # noqa: BLE001 — never break agent startup
            logger.warning("pg-sessions: failed to connect on init: %s", exc)
            return None

    return _store


def _check_available() -> Any:
    """Gate tool exposure: True only when PG_SESSIONS_URL is configured."""
    if not os.environ.get("PG_SESSIONS_URL", ""):
        return (
            False,
            "Set PG_SESSIONS_URL to enable pg-sessions "
            "(format: postgresql://user:pass@host:port/dbname)",
        )
    return True


# ── Hook callbacks ─────────────────────────────────────────────────────────


def _on_session_start(session_id: str, model: str = "", platform: str = "", **kwargs: Any) -> None:
    """Record a new session row when Hermes starts a session."""
    store = _get_store()
    if store is None:
        return

    user_id = kwargs.get("user_id", "") or ""
    provider = model.split("/")[0] if "/" in model else ""
    clean_model = model.split("/")[-1] if "/" in model else model

    try:
        store.create_session(
            session_id=session_id,
            platform=platform or "",
            user_id=user_id,
            model=clean_model,
            provider=provider,
            metadata={"model_full": model} if model else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("pg-sessions: on_session_start skipped: %s", exc)


def _post_llm_call(
    session_id: str,
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: Optional[list] = None,
    model: str = "",
    platform: str = "",
    **kwargs: Any,
) -> None:
    """Persist the assistant turn after each LLM call (non-blocking).

    Only the assistant response is stored: the user message of turn N is
    already captured as part of turn N-1's context, and storing both risks
    duplicates when the gateway replays a turn.
    """
    store = _get_store()
    if store is None:
        return

    try:
        history = conversation_history or []
        turn_index = len([m for m in history if m.get("role") in ("user", "assistant")])
        clean_model = model.split("/")[-1] if "/" in model else model

        if assistant_response:
            store.store_turn(
                session_id=session_id,
                turn_index=turn_index,
                role="assistant",
                content=assistant_response[:50000],
                model=clean_model,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("pg-sessions: post_llm_call skipped: %s", exc)


def _on_session_end(
    session_id: str,
    completed: bool = True,
    interrupted: bool = False,
    model: str = "",
    platform: str = "",
    **kwargs: Any,
) -> None:
    """Finalize the session row: flush pending turns, set status."""
    store = _get_store()
    if store is None:
        return

    status = "completed" if completed else ("interrupted" if interrupted else "completed")

    try:
        store.end_session(session_id=session_id, status=status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("pg-sessions: on_session_end skipped: %s", exc)


def _on_session_reset(session_id: str, platform: str = "", **kwargs: Any) -> None:
    """Mark the outgoing session as interrupted on /new or /reset."""
    store = _get_store()
    if store is None:
        return

    try:
        store.end_session(session_id=session_id, status="interrupted")
    except Exception as exc:  # noqa: BLE001
        logger.debug("pg-sessions: on_session_reset skipped: %s", exc)


# ── Tool handler wiring ────────────────────────────────────────────────────


def _make_handler(fn: Any) -> Any:
    """Bind the store singleton into a tool handler."""

    def handler(args: dict, **kwargs: Any) -> str:
        return fn(_get_store(), args, **kwargs)

    return handler


# ── Registration ───────────────────────────────────────────────────────────


def register(ctx: Any) -> None:
    """Register pg-sessions tools, hooks, and CLI commands.

    Called by the Hermes PluginManager when the plugin is enabled. Tool
    registration is unconditional — ``check_fn`` controls visibility so the
    model only sees ``sessions_*`` when a DSN is configured.
    """
    store = _get_store()

    # -- Tools --
    ctx.register_tool(
        name="sessions_query",
        toolset="pg_sessions",
        schema=schemas.SESSIONS_QUERY,
        handler=_make_handler(tools.handle_sessions_query),
        check_fn=_check_available,
        emoji="🔍",
    )
    ctx.register_tool(
        name="sessions_get",
        toolset="pg_sessions",
        schema=schemas.SESSIONS_GET,
        handler=_make_handler(tools.handle_sessions_get),
        check_fn=_check_available,
        emoji="📄",
    )
    ctx.register_tool(
        name="sessions_stats",
        toolset="pg_sessions",
        schema=schemas.SESSIONS_STATS,
        handler=_make_handler(tools.handle_sessions_stats),
        check_fn=_check_available,
        emoji="📊",
    )
    ctx.register_tool(
        name="sessions_migrate",
        toolset="pg_sessions",
        schema=schemas.SESSIONS_MIGRATE,
        handler=_make_handler(tools.handle_sessions_migrate),
        check_fn=_check_available,
        emoji="📦",
    )

    # -- Lifecycle hooks --
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)

    # -- CLI subcommand --
    ctx.register_cli_command(
        name="pg-sessions",
        help="Manage PostgreSQL session storage",
        setup_fn=cli.setup_cli,
        handler_fn=cli.handler_fn,
        description=(
            "Connect, inspect, and migrate Hermes sessions to PostgreSQL. "
            "Compatible with Neon, Supabase, Aiven, RDS, and standard PostgreSQL."
        ),
    )

    logger.info(
        "pg-sessions: registered (connected=%s)", bool(store and store._connected)
    )


# Imported last so the module-level symbol above is resolvable inside register().
from . import cli  # noqa: E402  (deliberate late import)
