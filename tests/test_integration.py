"""Integration tests against a real PostgreSQL instance.

Skipped unless ``PG_SESSIONS_URL_TEST`` is set. See README for a one-liner
that starts a throwaway PostgreSQL with Docker.
"""

from __future__ import annotations

import json
import uuid

import pytest

from pg_sessions.store import PGSessionStore


@pytest.fixture
def store(test_dsn: str):
    s = PGSessionStore()
    s.connect(test_dsn)
    _truncate(test_dsn)
    yield s
    s.shutdown()


def _truncate(dsn: str) -> None:
    """Clear both tables between tests so assertions are deterministic."""
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM pg_session_turns")
        cur.execute("DELETE FROM pg_sessions")
        conn.commit()
    finally:
        conn.close()


def _sid() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"


# ── Connection ─────────────────────────────────────────────────────────────


def test_connect_creates_tables(store: PGSessionStore) -> None:
    assert store._connected is True
    assert store._provider == "standard"


def test_connect_auto_appends_sslmode_for_managed_providers() -> None:
    """Known hosted providers get sslmode=require appended automatically."""
    s = PGSessionStore()
    # Detection-level check: no live connection attempted here.
    assert s.detect_provider("postgresql://u:p@ep-x.neon.tech/db")[0] == "neon"


# ── Session lifecycle ──────────────────────────────────────────────────────


def test_create_and_read_session(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli", user_id="u1", model="deepseek-v4", provider="deepseek")

    payload = json.loads(store.query_sessions(limit=10))
    assert payload["total"] == 1
    session = payload["sessions"][0]
    assert session["id"] == sid
    assert session["platform"] == "cli"
    assert session["status"] == "active"


def test_store_turns_and_read_back(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.store_turn(session_id=sid, turn_index=0, role="user", content="hello")
    store.store_turn(session_id=sid, turn_index=1, role="assistant", content="hi there")
    store.flush()

    payload = json.loads(store.get_session(sid))
    assert payload["total_turns"] == 2
    assert [t["role"] for t in payload["turns"]] == ["user", "assistant"]
    assert payload["turns"][0]["content"] == "hello"


def test_turn_count_stays_in_sync(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    for i in range(5):
        store.store_turn(session_id=sid, turn_index=i, role="user", content=f"turn {i}")
    store.flush()

    payload = json.loads(store.get_session(sid))
    assert payload["session"]["turn_count"] == 5


def test_tool_calls_round_trip_as_json(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.store_turn(
        session_id=sid,
        turn_index=0,
        role="assistant",
        content="searching",
        tool_calls=[{"name": "web_search", "arguments": {"query": "madrid weather"}}],
        tool_results=[{"success": True}],
    )
    store.flush()

    turn = json.loads(store.get_session(sid))["turns"][0]
    assert turn["tool_calls"][0]["name"] == "web_search"
    assert turn["tool_calls"][0]["arguments"]["query"] == "madrid weather"
    assert turn["tool_results"][0]["success"] is True


def test_end_session_sets_status_and_title(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.store_turn(session_id=sid, turn_index=0, role="user", content="q")
    store.end_session(sid, status="completed", title="Weather Q&A")

    session = json.loads(store.get_session(sid))["session"]
    assert session["status"] == "completed"
    assert session["title"] == "Weather Q&A"
    assert session["ended_at"]


def test_end_session_interrupted(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.end_session(sid, status="interrupted")

    assert json.loads(store.get_session(sid))["session"]["status"] == "interrupted"


def test_create_session_is_idempotent(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.create_session(sid, platform="cli")  # must not raise

    assert json.loads(store.query_sessions(limit=10))["total"] == 1


# ── Query / filtering ──────────────────────────────────────────────────────


def test_query_by_platform(store: PGSessionStore) -> None:
    store.create_session(_sid(), platform="telegram")
    store.create_session(_sid(), platform="cli")

    payload = json.loads(store.query_sessions(platform="telegram"))
    assert payload["total"] == 1
    assert payload["sessions"][0]["platform"] == "telegram"


def test_query_by_status(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.create_session(_sid(), platform="cli")
    store.end_session(sid, status="completed")

    payload = json.loads(store.query_sessions(status="completed"))
    assert payload["total"] == 1
    assert payload["sessions"][0]["id"] == sid


def test_full_text_search_matches_title(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.end_session(sid, status="completed", title="Barcelona weather report")

    payload = json.loads(store.query_sessions(query_text="Barcelona"))
    assert payload["total"] == 1
    assert payload["sessions"][0]["id"] == sid


def test_full_text_search_matches_turn_content(store: PGSessionStore) -> None:
    """Text search must reach inside transcripts, not just session titles."""
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.store_turn(
        session_id=sid,
        turn_index=0,
        role="user",
        content="what is the capital of Portugal",
    )
    store.flush()

    payload = json.loads(store.query_sessions(query_text="Portugal"))
    assert payload["total"] == 1
    assert payload["sessions"][0]["id"] == sid


def test_query_pagination(store: PGSessionStore) -> None:
    for _ in range(5):
        store.create_session(_sid(), platform="cli")

    page1 = json.loads(store.query_sessions(limit=2, offset=0))
    page2 = json.loads(store.query_sessions(limit=2, offset=2))

    assert page1["total"] == 5
    assert len(page1["sessions"]) == 2
    assert len(page2["sessions"]) == 2
    ids1 = {s["id"] for s in page1["sessions"]}
    ids2 = {s["id"] for s in page2["sessions"]}
    assert ids1.isdisjoint(ids2), "pagination must not repeat rows"


def test_query_empty_store_returns_zero(store: PGSessionStore) -> None:
    payload = json.loads(store.query_sessions())
    assert payload["total"] == 0
    assert payload["sessions"] == []


def test_since_days_filter(store: PGSessionStore) -> None:
    store.create_session(_sid(), platform="cli")

    # A 7-day window includes a row created just now
    recent = json.loads(store.query_sessions(since_days=7))
    assert "error" not in recent, recent
    assert recent["total"] == 1

    # All-time (since_days=0) is the unfiltered path
    all_time = json.loads(store.query_sessions(since_days=0))
    assert "error" not in all_time, all_time
    assert all_time["total"] == 1


def test_since_days_filter_excludes_old_rows(store: PGSessionStore, test_dsn: str) -> None:
    """A row backdated beyond the window must drop out."""
    import psycopg2

    sid = _sid()
    store.create_session(sid, platform="cli")

    conn = psycopg2.connect(test_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pg_sessions SET created_at = NOW() - 30 * INTERVAL '1 day' WHERE id = %s",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()

    assert json.loads(store.query_sessions(since_days=7))["total"] == 0
    assert json.loads(store.query_sessions(since_days=60))["total"] == 1


def test_since_days_combines_with_other_filters(store: PGSessionStore) -> None:
    """Multiple filters must compose without breaking parameter ordering."""
    store.create_session(_sid(), platform="telegram")
    store.create_session(_sid(), platform="cli")

    payload = json.loads(store.query_sessions(platform="cli", since_days=7, limit=10))
    assert "error" not in payload, payload
    assert payload["total"] == 1
    assert payload["sessions"][0]["platform"] == "cli"


# ── Stats ──────────────────────────────────────────────────────────────────


def test_stats_counts(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    for i in range(3):
        store.store_turn(
            session_id=sid, turn_index=i, role="user", content=f"t{i}", token_count=10
        )
    store.flush()

    stats = json.loads(store.get_stats(since_days=0))
    assert stats["total_sessions"] == 1
    assert stats["total_turns"] == 3
    assert stats["total_tokens"] == 30
    assert stats["avg_turns_per_session"] == 3.0
    assert stats["by_platform"]["cli"] == 1


def test_stats_on_empty_store(store: PGSessionStore) -> None:
    stats = json.loads(store.get_stats(since_days=0))
    assert stats["total_sessions"] == 0
    assert stats["total_turns"] == 0
    assert stats["avg_turns_per_session"] == 0


# ── Concurrency ────────────────────────────────────────────────────────────


def test_many_sessions(store: PGSessionStore) -> None:
    ids = [_sid() for _ in range(10)]
    for sid in ids:
        store.create_session(sid, platform="telegram")
        store.store_turn(session_id=sid, turn_index=0, role="user", content="x")
    store.flush()

    payload = json.loads(store.query_sessions(limit=100))
    assert payload["total"] == 10
    assert {s["id"] for s in payload["sessions"]} == set(ids)


def test_rapid_sequential_writes(store: PGSessionStore) -> None:
    """Back-to-back turns must not be dropped by the daemon-thread flush."""
    sid = _sid()
    store.create_session(sid, platform="cli")
    for i in range(25):
        store.store_turn(session_id=sid, turn_index=i, role="user", content=f"turn {i}")
    store.flush()

    payload = json.loads(store.get_session(sid, limit=500))
    assert payload["total_turns"] == 25


# ── Error handling ─────────────────────────────────────────────────────────


def test_get_missing_session_returns_error(store: PGSessionStore) -> None:
    payload = json.loads(store.get_session("does-not-exist"))
    assert "error" in payload
    assert "not found" in payload["error"]


def test_unicode_content_round_trips(store: PGSessionStore) -> None:
    sid = _sid()
    store.create_session(sid, platform="cli")
    text = "こんにちは — naïve café ☕ emoji 🎯"
    store.store_turn(session_id=sid, turn_index=0, role="user", content=text)
    store.flush()

    assert json.loads(store.get_session(sid))["turns"][0]["content"] == text


def test_long_content_is_capped(store: PGSessionStore) -> None:
    """Guards against one runaway turn bloating the transcript table."""
    sid = _sid()
    store.create_session(sid, platform="cli")
    store.store_turn(session_id=sid, turn_index=0, role="user", content="x" * 200_000)
    store.flush()

    stored = json.loads(store.get_session(sid))["turns"][0]["content"]
    assert len(stored) <= 100_000
