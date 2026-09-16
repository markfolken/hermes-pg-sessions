"""End-to-end verification of the HOOK path — the real Hermes integration point.

The other test modules exercise the store directly and register() against a
FakeCtx. This one drives the actual hook callbacks Hermes invokes during a
session, with a live PostgreSQL behind them.
"""

from __future__ import annotations

import json
import uuid

import pytest

import pg_sessions


@pytest.fixture
def hooked(test_dsn, monkeypatch):
    """Wire the plugin's hooks against live PostgreSQL."""
    import psycopg2

    # Reset the module singleton so _get_store() connects fresh
    monkeypatch.setenv("PG_SESSIONS_URL", test_dsn)
    monkeypatch.setattr(pg_sessions, "_store", None)

    store = pg_sessions._get_store()
    assert store is not None, "store failed to initialise from PG_SESSIONS_URL"
    assert store._connected

    conn = psycopg2.connect(test_dsn)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM pg_session_turns")
        cur.execute("DELETE FROM pg_sessions")
        conn.commit()
    finally:
        conn.close()

    yield store

    store.shutdown()
    monkeypatch.setattr(pg_sessions, "_store", None)


def _sid() -> str:
    return f"hook-{uuid.uuid4().hex[:12]}"


def test_session_start_hook_creates_row(hooked) -> None:
    sid = _sid()
    pg_sessions._on_session_start(
        session_id=sid,
        model="openrouter/deepseek/deepseek-v4.1-flash",
        platform="telegram",
        user_id="7612729128",
    )

    payload = json.loads(hooked.query_sessions(limit=10))
    assert payload["total"] == 1
    session = payload["sessions"][0]
    assert session["id"] == sid
    assert session["platform"] == "telegram"
    # Provider is split out of the model string
    assert session["model"] == "deepseek-v4.1-flash"


def test_post_llm_call_hook_stores_turn(hooked) -> None:
    sid = _sid()
    pg_sessions._on_session_start(session_id=sid, model="gpt-4", platform="cli")
    pg_sessions._post_llm_call(
        session_id=sid,
        user_message="what is 2+2",
        assistant_response="4",
        conversation_history=[{"role": "user", "content": "what is 2+2"}],
        model="openrouter/gpt-4",
        platform="cli",
    )
    hooked.flush()

    payload = json.loads(hooked.get_session(sid))
    assert payload["total_turns"] == 1
    turn = payload["turns"][0]
    assert turn["role"] == "assistant"
    assert turn["content"] == "4"


def test_full_session_lifecycle(hooked) -> None:
    """start -> several turns -> end, as Hermes drives it."""
    sid = _sid()
    pg_sessions._on_session_start(session_id=sid, model="openrouter/x/y", platform="telegram")

    history: list = []
    for i in range(4):
        history.append({"role": "user", "content": f"question {i}"})
        pg_sessions._post_llm_call(
            session_id=sid,
            user_message=f"question {i}",
            assistant_response=f"answer {i}",
            conversation_history=list(history),
            model="openrouter/x/y",
            platform="telegram",
        )
        history.append({"role": "assistant", "content": f"answer {i}"})

    pg_sessions._on_session_end(
        session_id=sid, completed=True, interrupted=False, model="x/y", platform="telegram"
    )

    payload = json.loads(hooked.get_session(sid))
    session = payload["session"]
    assert session["status"] == "completed"
    assert session["turn_count"] == 4
    assert {t["content"] for t in payload["turns"]} == {f"answer {i}" for i in range(4)}


def test_session_reset_marks_interrupted(hooked) -> None:
    sid = _sid()
    pg_sessions._on_session_start(session_id=sid, model="m", platform="cli")
    pg_sessions._on_session_reset(session_id=sid, platform="cli")

    assert json.loads(hooked.get_session(sid))["session"]["status"] == "interrupted"


def test_hooks_survive_missing_store(monkeypatch) -> None:
    """A dead/unconfigured backend must never raise into the agent loop."""
    monkeypatch.delenv("PG_SESSIONS_URL", raising=False)
    monkeypatch.setattr(pg_sessions, "_store", None)

    # None of these may raise
    pg_sessions._on_session_start(session_id="x", model="m", platform="cli")
    pg_sessions._post_llm_call(session_id="x", user_message="a", assistant_response="b")
    pg_sessions._on_session_end(session_id="x", completed=True)
    pg_sessions._on_session_reset(session_id="x")


def test_hooks_survive_broken_backend(monkeypatch, test_dsn) -> None:
    """Even with a store object whose pool is dead, hooks stay silent."""
    monkeypatch.setenv("PG_SESSIONS_URL", test_dsn)
    monkeypatch.setattr(pg_sessions, "_store", None)
    store = pg_sessions._get_store()
    assert store is not None

    # Kill the pool underneath the store
    store._connected = False
    store._pool = None

    pg_sessions._on_session_start(session_id="broken-1", model="m", platform="cli")
    pg_sessions._post_llm_call(session_id="broken-1", user_message="a", assistant_response="b")
    pg_sessions._on_session_end(session_id="broken-1", completed=True)
    # Reaching here means no exception escaped

    store.shutdown()
    monkeypatch.setattr(pg_sessions, "_store", None)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_flush_thread_never_raises(monkeypatch, test_dsn) -> None:
    """The daemon flush thread must swallow backend failures.

    Regression: _get_conn() used to sit outside the try in _batch_insert(),
    so a dead backend raised inside the daemon thread — printing a traceback
    and silently dropping the buffered turn. The filterwarnings marker turns
    any such thread exception into a test failure.
    """
    import threading
    import time

    monkeypatch.setenv("PG_SESSIONS_URL", test_dsn)
    monkeypatch.setattr(pg_sessions, "_store", None)
    store = pg_sessions._get_store()
    assert store is not None

    store._connected = False
    store._pool = None

    pg_sessions._post_llm_call(
        session_id="broken-2", user_message="a", assistant_response="b"
    )

    # Give the daemon thread time to run and (if buggy) raise
    for _ in range(30):
        if all(
            not t.is_alive() for t in threading.enumerate() if t.name == "pg-sessions-sync"
        ):
            break
        time.sleep(0.05)
    time.sleep(0.2)

    store.shutdown()
    monkeypatch.setattr(pg_sessions, "_store", None)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_failed_flush_requeues_turn(monkeypatch, test_dsn) -> None:
    """A turn that fails to persist stays buffered for a later retry."""
    import time

    monkeypatch.setenv("PG_SESSIONS_URL", test_dsn)
    monkeypatch.setattr(pg_sessions, "_store", None)
    store = pg_sessions._get_store()
    assert store is not None

    # Point the store at a dead pool so the flush fails, then repair it
    store._connected = False
    store._pool = None

    pg_sessions._post_llm_call(
        session_id="requeue-1", user_message="a", assistant_response="keep me"
    )
    time.sleep(0.5)

    # The turn must still be buffered, not lost
    assert len(store._pending_turns) == 1
    assert store._pending_turns[0]["content"] == "keep me"

    store._pending_turns.clear()
    store.shutdown()
    monkeypatch.setattr(pg_sessions, "_store", None)


def test_pending_queue_is_bounded() -> None:
    """An outage must not grow the buffer without limit."""
    from pg_sessions import store as store_module

    assert store_module._MAX_PENDING_TURNS > 0
    assert store_module._MAX_PENDING_TURNS <= 100_000


def test_empty_assistant_response_not_stored(hooked) -> None:
    """A tool-only turn with no text must not create an empty row."""
    sid = _sid()
    pg_sessions._on_session_start(session_id=sid, model="m", platform="cli")
    pg_sessions._post_llm_call(
        session_id=sid,
        user_message="run something",
        assistant_response="",
        conversation_history=[],
        model="m",
        platform="cli",
    )
    hooked.flush()

    assert json.loads(hooked.get_session(sid))["total_turns"] == 0
