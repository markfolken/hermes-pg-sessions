"""Store unit tests — provider detection, circuit breaker, SQL safety.

These need no database connection.
"""

from __future__ import annotations

import time

import pytest

from pg_sessions import store as store_module
from pg_sessions.store import PGSessionStore


@pytest.fixture
def store() -> PGSessionStore:
    return PGSessionStore()


# ── Provider detection ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgresql://u:p@ep-cool-123.us-east-2.aws.neon.tech/neondb", ("neon", False)),
        ("postgresql://u:p@ep-cool-123-pooler.us-east-2.aws.neon.tech/neondb", ("neon", True)),
        (
            "postgresql://u:p@ep-cool-123-pooler.us-east-2.aws.neon.tech/neondb?pgbouncer=true",
            ("neon", True),
        ),
        ("postgresql://postgres:p@db.abcdefg.supabase.co:6543/postgres", ("supabase", True)),
        ("postgresql://postgres:p@db.abcdefg.supabase.co:5432/postgres", ("supabase", False)),
        (
            "postgresql://postgres:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres",
            ("supabase", True),
        ),
        ("postgresql://u:p@my-project.aivencloud.com:12345/defaultdb", ("aiven", False)),
        ("postgresql://u:p@mydb.abc123.us-east-1.rds.amazonaws.com:5432/postgres", ("rds", False)),
        ("postgresql://u:p@db.ondigitalocean.com:25060/defaultdb", ("digitalocean", False)),
        ("postgresql://u:p@localhost:5432/dbname", ("standard", False)),
        ("postgresql://u:p@10.0.0.5:5432/dbname", ("standard", False)),
    ],
)
def test_detect_provider(store: PGSessionStore, dsn: str, expected: tuple) -> None:
    assert store.detect_provider(dsn) == expected


def test_detect_provider_is_case_insensitive(store: PGSessionStore) -> None:
    assert store.detect_provider("postgresql://u:p@EP-X.US-EAST-2.AWS.NEON.TECH/db") == ("neon", False)


# ── Circuit breaker ────────────────────────────────────────────────────────


def test_breaker_starts_closed(store: PGSessionStore) -> None:
    assert store._is_breaker_open() is False


def test_breaker_opens_after_threshold(store: PGSessionStore) -> None:
    for _ in range(store_module._BREAKER_THRESHOLD):
        store._record_failure(RuntimeError("boom"))

    assert store._is_breaker_open() is True


def test_breaker_stays_closed_below_threshold(store: PGSessionStore) -> None:
    for _ in range(store_module._BREAKER_THRESHOLD - 1):
        store._record_failure(RuntimeError("boom"))

    assert store._is_breaker_open() is False


def test_breaker_success_resets_counter(store: PGSessionStore) -> None:
    store._record_failure(RuntimeError("boom"))
    store._record_failure(RuntimeError("boom"))
    store._record_success()
    store._record_failure(RuntimeError("boom"))

    assert store._is_breaker_open() is False


def test_breaker_closes_after_cooldown(store: PGSessionStore, monkeypatch) -> None:
    for _ in range(store_module._BREAKER_THRESHOLD):
        store._record_failure(RuntimeError("boom"))

    assert store._is_breaker_open() is True

    # Jump past the cooldown window
    future = time.monotonic() + store_module._BREAKER_COOLDOWN_SECS + 1
    monkeypatch.setattr(time, "monotonic", lambda: future)

    assert store._is_breaker_open() is False


def test_breaker_constants() -> None:
    assert store_module._BREAKER_THRESHOLD == 5
    assert store_module._BREAKER_COOLDOWN_SECS == 120


# ── Reads fail fast when the breaker is open ───────────────────────────────


def test_queries_short_circuit_when_breaker_open(store: PGSessionStore) -> None:
    for _ in range(store_module._BREAKER_THRESHOLD):
        store._record_failure(RuntimeError("boom"))

    import json

    for method, kwargs in [
        (store.query_sessions, {}),
        (store.get_session, {"session_id": "x"}),
        (store.get_stats, {}),
    ]:
        payload = json.loads(method(**kwargs))
        assert "error" in payload
        assert "circuit breaker" in payload["error"]


# ── Unconnected store degrades cleanly ─────────────────────────────────────


def test_flush_without_connection_is_safe(store: PGSessionStore) -> None:
    """flush() on an unconnected store must not raise."""
    store.flush()


def test_shutdown_is_idempotent(store: PGSessionStore) -> None:
    store.shutdown()
    store.shutdown()


# ── Schema DDL is extension-free ───────────────────────────────────────────


def test_no_pg_extension_dependency() -> None:
    """Managed providers often forbid CREATE EXTENSION.

    Checks for the actual SQL statements, not the words: ``pgcrypto`` and
    ``uuid-ossp`` legitimately appear in explanatory comments.
    """
    source = open(store_module.__file__).read()
    assert "CREATE EXTENSION" not in source
    assert "create extension" not in source.lower()


def test_expected_tables_declared() -> None:
    source = open(store_module.__file__).read()
    assert "CREATE TABLE IF NOT EXISTS pg_sessions" in source
    assert "CREATE TABLE IF NOT EXISTS pg_session_turns" in source


def test_uses_python_generated_ids_not_db_extensions() -> None:
    """IDs must come from Python so no extension is needed server-side."""
    import uuid as _uuid

    assert hasattr(_uuid, "uuid4"), "stdlib uuid must be available"
    source = open(store_module.__file__).read()
    # No generated-UUID function calls in SQL
    assert "gen_random_uuid()" not in source
    assert "uuid_generate_v4()" not in source


def test_cascade_delete_declared() -> None:
    source = open(store_module.__file__).read()
    assert "ON DELETE CASCADE" in source
