"""PostgreSQL session store — compatible with Neon, Supabase, and standard PostgreSQL.

Handles:
- Neon auto-pause cold starts (retry + backoff)
- PGBouncer transaction mode (no SET, no session state)
- SSL-only connections
- No extension dependencies (Python UUIDs, not pgcrypto)
- Connection pooling configuration per provider
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("plugins.pg-sessions.store")

# --- Circuit breaker ---
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120


class PGSessionStore:
    """PostgreSQL-backed session store with circuit breaker and provider detection."""

    def __init__(self):
        self._conn = None
        self._pool = None
        self._dsn = ""
        self._connected = False
        self._provider = "unknown"  # neon, supabase, standard
        self._is_pooled = False  # behind PGBouncer?
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_lock = threading.Lock()
        self._sync_thread = None
        self._sync_lock = threading.Lock()
        self._pending_turns = []
        self._pending_lock = threading.Lock()

    # ── Connection management ──

    def detect_provider(self, dsn: str) -> tuple[str, bool]:
        """Detect the PostgreSQL provider and whether it's behind PGBouncer.

        Returns (provider_name, is_pooled).
        """
        dsn_lower = dsn.lower()
        is_pooled = "pgbouncer=true" in dsn_lower

        if "neon.tech" in dsn_lower:
            return ("neon", is_pooled or "pooler" in dsn_lower)
        if "supabase.co" in dsn_lower or "supabase.com" in dsn_lower or "pooler.supabase" in dsn_lower:
            return ("supabase", is_pooled or ":6543" in dsn)
        if "aivencloud.com" in dsn_lower:
            return ("aiven", is_pooled)
        if "rds.amazonaws.com" in dsn_lower:
            return ("rds", is_pooled)
        if "digitaloceanspaces.com" in dsn_lower or "db.ondigitalocean.com" in dsn_lower:
            return ("digitalocean", is_pooled)
        return ("standard", is_pooled)

    def connect(self, dsn: str, min_conn: int = 1, max_conn: int = 5) -> None:
        """Establish connection and create tables.

        Uses a single connection by default (turn storage is sequential).
        For high-throughput deployments, increase min_conn/max_conn.
        """
        import psycopg2
        from psycopg2 import pool

        self._provider, self._is_pooled = self.detect_provider(dsn)

        # Auto-append sslmode if not present for known providers
        if not dsn_lower if False else "sslmode=" not in dsn.lower():
            if self._provider in ("neon", "supabase", "aiven", "rds"):
                dsn += "&sslmode=require" if "?" in dsn else "?sslmode=require"

        logger.info(
            "pg-sessions: connecting to %s (provider=%s, pooled=%s)",
            self._provider, self._provider, self._is_pooled
        )

        # ThreadedConnectionPool for safe multi-hook access
        try:
            self._pool = pool.ThreadedConnectionPool(min_conn, max_conn, dsn)
        except Exception as exc:
            logger.error("pg-sessions: connection pool failed: %s", exc)
            raise

        self._dsn = dsn
        self._connected = True
        self._create_tables()

        logger.info("pg-sessions: connected (provider=%s, pooled=%s)", self._provider, self._is_pooled)

    def _get_conn(self):
        """Get a connection from the pool with Neon cold-start retry."""
        if not self._connected or self._pool is None:
            raise RuntimeError("pg-sessions: not connected")

        max_retries = 3 if self._provider == "neon" else 1
        last_exc = None

        for attempt in range(max_retries):
            try:
                conn = self._pool.getconn()
                if conn is not None:
                    # In pooled mode, avoid session-state leakage
                    if self._is_pooled:
                        conn.set_session(autocommit=True)
                    return conn
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "pg-sessions: getconn attempt %d/%d failed: %s (provider=%s)",
                    attempt + 1, max_retries, exc, self._provider,
                )
                if attempt < max_retries - 1:
                    # Neon cold start: backoff 500ms, 1s, then full retry
                    time.sleep(0.5 * (attempt + 1))

        raise last_exc or RuntimeError("pg-sessions: failed to get connection")

    def _put_conn(self, conn) -> None:
        """Return a connection to the pool."""
        try:
            self._pool.putconn(conn)
        except Exception:
            pass  # pool may be closed during shutdown

    def shutdown(self) -> None:
        """Close all connections."""
        self._connected = False
        if self._pool is not None:
            try:
                self._pool.closeall()
            except Exception:
                pass
            self._pool = None
        logger.info("pg-sessions: shut down")

    # ── Circuit breaker ──

    def _is_breaker_open(self) -> bool:
        with self._breaker_lock:
            if (self._consecutive_failures >= _BREAKER_THRESHOLD
                    and time.monotonic() < self._breaker_open_until):
                return True
            if self._consecutive_failures >= _BREAKER_THRESHOLD:
                self._consecutive_failures = 0
            return False

    def _record_success(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self, exc: Exception) -> None:
        with self._breaker_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
                logger.warning(
                    "pg-sessions: circuit breaker opened for %ds (failures=%d): %s",
                    _BREAKER_COOLDOWN_SECS, self._consecutive_failures, exc,
                )

    # ── Schema (vanilla SQL — no extensions needed) ──

    def _create_tables(self) -> None:
        """Create tables if they don't exist. Pure PG-compatible SQL."""
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pg_sessions (
                    id            TEXT PRIMARY KEY,
                    title         TEXT DEFAULT '',
                    platform      TEXT DEFAULT '',
                    user_id       TEXT DEFAULT '',
                    model         TEXT DEFAULT '',
                    provider      TEXT DEFAULT '',
                    status        TEXT DEFAULT 'active',
                    turn_count    INTEGER DEFAULT 0,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ DEFAULT NOW(),
                    ended_at      TIMESTAMPTZ,
                    metadata      JSONB DEFAULT '{}'::jsonb
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pg_session_turns (
                    id            BIGSERIAL PRIMARY KEY,
                    session_id    TEXT NOT NULL REFERENCES pg_sessions(id) ON DELETE CASCADE,
                    turn_index    INTEGER NOT NULL,
                    role          TEXT NOT NULL,
                    content       TEXT DEFAULT '',
                    tool_calls    JSONB DEFAULT '[]'::jsonb,
                    tool_results  JSONB DEFAULT '[]'::jsonb,
                    model         TEXT DEFAULT '',
                    token_count   INTEGER DEFAULT 0,
                    duration_ms   INTEGER DEFAULT 0,
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_turns_session
                    ON pg_session_turns(session_id, turn_index);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_created
                    ON pg_sessions(created_at DESC);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_status
                    ON pg_sessions(status);
            """)
            # Full-text search index on session titles
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_title_fts
                    ON pg_sessions USING gin(to_tsvector('english', coalesce(title, '')));
            """)
            conn.commit()
            logger.info("pg-sessions: tables verified")
        except Exception as exc:
            conn.rollback()
            logger.error("pg-sessions: table creation failed: %s", exc)
            raise
        finally:
            self._put_conn(conn)

    def flush(self) -> None:
        """Synchronously flush all pending turns. Blocks until done."""
        with self._sync_lock:
            prev = self._sync_thread
            if prev and prev.is_alive():
                prev.join(timeout=10.0)
            self._batch_insert()

    # ── Session lifecycle hooks ──

    def create_session(self, session_id: str, platform: str = "", user_id: str = "",
                       model: str = "", provider: str = "", metadata: Optional[dict] = None) -> None:
        """Record a new session (called from on_session_start)."""
        if self._is_breaker_open():
            return

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO pg_sessions (id, platform, user_id, model, provider, metadata, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET
                    updated_at = NOW()
                """,
                (session_id, platform, user_id, model, provider,
                 json.dumps(metadata or {})),
            )
            conn.commit()
            self._record_success()
        except Exception as exc:
            conn.rollback()
            self._record_failure(exc)
            logger.warning("pg-sessions: create_session failed: %s", exc)
        finally:
            self._put_conn(conn)

    def store_turn(self, session_id: str, turn_index: int, role: str, content: str,
                   tool_calls: Optional[list] = None, tool_results: Optional[list] = None,
                   model: str = "", token_count: int = 0, duration_ms: int = 0) -> None:
        """Store a conversation turn (called from post_llm_call).

        This queues the turn for non-blocking batch insert on a daemon thread.
        """
        if self._is_breaker_open():
            return

        turn = {
            "session_id": session_id,
            "turn_index": turn_index,
            "role": role,
            "content": content,
            "tool_calls": json.dumps(tool_calls or []),
            "tool_results": json.dumps(tool_results or []),
            "model": model,
            "token_count": token_count,
            "duration_ms": duration_ms,
        }

        with self._pending_lock:
            self._pending_turns.append(turn)

        # Spawn daemon thread for non-blocking flush
        def _flush():
            with self._sync_lock:
                prev = self._sync_thread
                if prev and prev is not threading.current_thread() and prev.is_alive():
                    prev.join(timeout=5.0)
                self._batch_insert()
                self._update_session_turn_count(session_id)

        t = threading.Thread(target=_flush, daemon=True, name="pg-sessions-sync")
        with self._sync_lock:
            self._sync_thread = t
        t.start()

    def _batch_insert(self) -> None:
        """Flush all pending turns in one batch INSERT."""
        with self._pending_lock:
            if not self._pending_turns:
                return
            turns = list(self._pending_turns)
            self._pending_turns.clear()

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            import psycopg2.extras
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO pg_session_turns
                    (session_id, turn_index, role, content, tool_calls, tool_results, model, token_count, duration_ms)
                VALUES %s
                """,
                [
                    (
                        t["session_id"], t["turn_index"], t["role"],
                        t["content"], t["tool_calls"], t["tool_results"],
                        t["model"], t["token_count"], t["duration_ms"],
                    )
                    for t in turns
                ],
                template="(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
            )
            conn.commit()
            self._record_success()
        except Exception as exc:
            conn.rollback()
            self._record_failure(exc)
            logger.warning("pg-sessions: batch_insert failed: %s", exc)
            # Re-queue failed turns
            with self._pending_lock:
                self._pending_turns[:0] = turns
        finally:
            self._put_conn(conn)

    def _update_session_turn_count(self, session_id: str) -> None:
        """Update the turn count on the session record."""
        try:
            conn = self._get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE pg_sessions SET
                        turn_count = (SELECT COUNT(*) FROM pg_session_turns WHERE session_id = %s),
                        updated_at = NOW()
                    WHERE id = %s
                """,
                    (session_id, session_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                self._put_conn(conn)
        except Exception:
            pass  # best-effort

    def end_session(self, session_id: str, status: str = "completed",
                    title: str = "") -> None:
        """Mark a session as ended (called from on_session_end)."""
        if self._is_breaker_open():
            return

        # Flush any remaining pending turns first
        self.flush()

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE pg_sessions SET
                    status = %s,
                    ended_at = NOW(),
                    updated_at = NOW(),
                    title = COALESCE(NULLIF(%s, ''), title),
                    turn_count = (SELECT COUNT(*) FROM pg_session_turns WHERE session_id = %s)
                WHERE id = %s
                """,
                (status, title, session_id, session_id),
            )
            conn.commit()
            self._record_success()
        except Exception as exc:
            conn.rollback()
            self._record_failure(exc)
            logger.warning("pg-sessions: end_session failed: %s", exc)
        finally:
            self._put_conn(conn)

    # ── Query tools ──

    def query_sessions(self, query_text: str = "", platform: str = "",
                       limit: int = 20, offset: int = 0,
                       since_days: int = 0, status: str = "") -> str:
        """Search sessions. Returns JSON string."""
        if self._is_breaker_open():
            return json.dumps({"error": "storage unavailable (circuit breaker open)"})

        self.flush()

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            params = []
            conditions = []
            order = "created_at DESC"

            if since_days > 0:
                # Parametrising inside a quoted INTERVAL literal is invalid SQL
                # (produces INTERVAL ''7' days'). Multiply instead.
                conditions.append("created_at >= NOW() - %s::int * INTERVAL '1 day'")
                params.append(int(since_days))
            if platform:
                conditions.append("platform = %s")
                params.append(platform)
            if status:
                conditions.append("status = %s")
                params.append(status)
            if query_text:
                # Search session title/id AND turn content
                conditions.append(
                    "("
                    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(id, ''))"
                    "  @@ plainto_tsquery('english', %s)"
                    " OR EXISTS ("
                    "  SELECT 1 FROM pg_session_turns t"
                    "  WHERE t.session_id = pg_sessions.id"
                    "    AND to_tsvector('english', coalesce(t.content, ''))"
                    "        @@ plainto_tsquery('english', %s)"
                    ")"
                    ")"
                )
                params.append(query_text)
                params.append(query_text)

            where = " AND ".join(conditions) if conditions else "TRUE"

            cur.execute(
                f"SELECT id, title, platform, user_id, model, status, turn_count, created_at, ended_at "
                f"FROM pg_sessions WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            rows = cur.fetchall()
            cur.execute(
                f"SELECT COUNT(*) FROM pg_sessions WHERE {where}",
                params,
            )
            total = cur.fetchone()[0]

            results = [
                {
                    "id": r[0],
                    "title": r[1] or "",
                    "platform": r[2] or "",
                    "user_id": r[3] or "",
                    "model": r[4] or "",
                    "status": r[5] or "active",
                    "turn_count": r[6] or 0,
                    "created_at": str(r[7]) if r[7] else "",
                    "ended_at": str(r[8]) if r[8] else "",
                }
                for r in rows
            ]
            return json.dumps({"sessions": results, "total": total, "limit": limit, "offset": offset})

        except Exception as exc:
            self._record_failure(exc)
            return json.dumps({"error": str(exc)})
        finally:
            self._put_conn(conn)

    def get_session(self, session_id: str, limit: int = 50, offset: int = 0) -> str:
        """Get a session with its turns. Returns JSON string."""
        if self._is_breaker_open():
            return json.dumps({"error": "storage unavailable (circuit breaker open)"})

        self.flush()

        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # Session header
            cur.execute(
                "SELECT id, title, platform, user_id, model, provider, status, turn_count, created_at, ended_at, metadata "
                "FROM pg_sessions WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return json.dumps({"error": f"session not found: {session_id}"})

            session = {
                "id": row[0],
                "title": row[1] or "",
                "platform": row[2] or "",
                "user_id": row[3] or "",
                "model": row[4] or "",
                "provider": row[5] or "",
                "status": row[6] or "active",
                "turn_count": row[7] or 0,
                "created_at": str(row[8]) if row[8] else "",
                "ended_at": str(row[9]) if row[9] else "",
                "metadata": row[10] or {},
            }

            # Turns
            cur.execute(
                "SELECT turn_index, role, content, tool_calls, tool_results, model, token_count, duration_ms, created_at "
                "FROM pg_session_turns WHERE session_id = %s ORDER BY turn_index ASC LIMIT %s OFFSET %s",
                (session_id, limit, offset),
            )
            turns = [
                {
                    "turn_index": r[0],
                    "role": r[1],
                    "content": (r[2] or "")[:5000],  # truncate for context
                    "tool_calls": r[3] or [],
                    "tool_results": r[4] or [],
                    "model": r[5] or "",
                    "token_count": r[6] or 0,
                    "duration_ms": r[7] or 0,
                    "created_at": str(r[8]) if r[8] else "",
                }
                for r in cur.fetchall()
            ]

            # Total turn count for pagination
            cur.execute(
                "SELECT COUNT(*) FROM pg_session_turns WHERE session_id = %s",
                (session_id,),
            )
            total_turns = cur.fetchone()[0]

            return json.dumps({
                "session": session,
                "turns": turns,
                "total_turns": total_turns,
                "limit": limit,
                "offset": offset,
            })

        except Exception as exc:
            self._record_failure(exc)
            return json.dumps({"error": str(exc)})
        finally:
            self._put_conn(conn)

    def get_stats(self, since_days: int = 7) -> str:
        """Get usage statistics. Returns JSON string."""
        if self._is_breaker_open():
            return json.dumps({"error": "storage unavailable (circuit breaker open)"})

        self.flush()

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            params_single = []

            if since_days > 0:
                time_filter = "WHERE created_at >= NOW() - %s::int * INTERVAL '1 day'"
                time_params = [since_days]
            else:
                time_filter = ""
                time_params = []

            # All queries use parameterized time window
            if time_params:
                cur.execute(
                    "SELECT COUNT(*) FROM pg_sessions " + time_filter,
                    time_params,
                )
            else:
                cur.execute("SELECT COUNT(*) FROM pg_sessions")
            total_sessions = cur.fetchone()[0]

            if time_params:
                cur.execute(
                    "SELECT COUNT(*) FROM pg_session_turns t WHERE t.created_at >= NOW() - %s::int * INTERVAL '1 day'",
                    time_params,
                )
            else:
                cur.execute("SELECT COUNT(*) FROM pg_session_turns t")
            total_turns = cur.fetchone()[0]

            if time_params:
                cur.execute(
                    "SELECT COALESCE(SUM(token_count), 0) FROM pg_session_turns t WHERE t.created_at >= NOW() - %s::int * INTERVAL '1 day'",
                    time_params,
                )
            else:
                cur.execute("SELECT COALESCE(SUM(token_count), 0) FROM pg_session_turns t")
            total_tokens = cur.fetchone()[0]

            if time_params:
                cur.execute(
                    "SELECT status, COUNT(*) FROM pg_sessions " + time_filter + " GROUP BY status ORDER BY COUNT(*) DESC",
                    time_params,
                )
            else:
                cur.execute("SELECT status, COUNT(*) FROM pg_sessions GROUP BY status ORDER BY COUNT(*) DESC")
            by_status = dict(cur.fetchall())

            if time_params:
                cur.execute(
                    "SELECT platform, COUNT(*) FROM pg_sessions " + time_filter + " GROUP BY platform ORDER BY COUNT(*) DESC LIMIT 10",
                    time_params,
                )
            else:
                cur.execute("SELECT platform, COUNT(*) FROM pg_sessions GROUP BY platform ORDER BY COUNT(*) DESC LIMIT 10")
            by_platform = dict(cur.fetchall())

            # Active sessions
            if time_params:
                cur.execute(
                    "SELECT COUNT(*) FROM pg_sessions WHERE status = 'active' AND created_at >= NOW() - %s::int * INTERVAL '1 day'",
                    time_params,
                )
            else:
                cur.execute("SELECT COUNT(*) FROM pg_sessions WHERE status = 'active'")
            active_sessions = cur.fetchone()[0]

            # Avg turns per session
            if total_sessions > 0:
                avg_turns = round(total_turns / total_sessions, 1)
            else:
                avg_turns = 0

            return json.dumps({
                "provider": self._provider,
                "pooled": self._is_pooled,
                "connected": self._connected,
                "since_days": since_days,
                "total_sessions": total_sessions,
                "total_turns": total_turns,
                "total_tokens": total_tokens,
                "avg_turns_per_session": avg_turns,
                "active_sessions": active_sessions,
                "by_status": by_status,
                "by_platform": by_platform,
            })

        except Exception as exc:
            self._record_failure(exc)
            return json.dumps({"error": str(exc)})
        finally:
            self._put_conn(conn)

    def migrate_from_sqlite(self, state_db_path: str, dry_run: bool = False) -> str:
        """Migrate sessions from local Hermes SQLite state.db to PostgreSQL."""
        import sqlite3

        path = Path(state_db_path).expanduser()
        if not path.exists():
            return json.dumps({"error": f"state.db not found: {path}"})

        conn_sqlite = sqlite3.connect(str(path))
        conn_sqlite.row_factory = sqlite3.Row
        cur_sqlite = conn_sqlite.cursor()

        try:
            # Check for sessions table
            cur_sqlite.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            )
            if not cur_sqlite.fetchone():
                return json.dumps({"error": "no 'sessions' table in state.db"})

            # Count total
            cur_sqlite.execute("SELECT COUNT(*) FROM sessions")
            total = cur_sqlite.fetchone()[0]
            if total == 0:
                return json.dumps({"sessions_migrated": 0, "turns_migrated": 0, "message": "no sessions to migrate"})

            if dry_run:
                conn_sqlite.close()
                return json.dumps({
                    "dry_run": True,
                    "total_sessions": total,
                    "message": f"Would migrate {total} sessions. Run without dry_run to execute.",
                })

            # Migrate
            migrated_sessions = 0
            migrated_turns = 0

            cur_sqlite.execute(
                "SELECT id, title, platform, user_id, model, provider, status, created_at, ended_at FROM sessions ORDER BY created_at ASC"
            )
            conn_pg = self._get_conn()
            try:
                cur_pg = conn_pg.cursor()

                for row in cur_sqlite.fetchall():
                    sess_id = row["id"]
                    # Check if already migrated
                    cur_pg.execute("SELECT 1 FROM pg_sessions WHERE id = %s", (sess_id,))
                    if cur_pg.fetchone():
                        continue

                    cur_pg.execute(
                        """
                        INSERT INTO pg_sessions (id, title, platform, user_id, model, provider, status, created_at, updated_at, ended_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            sess_id,
                            row["title"] or "",
                            row["platform"] or "",
                            row["user_id"] or "",
                            row["model"] or "",
                            row["provider"] or "",
                            row["status"] or "completed",
                            row["created_at"] or datetime.now(timezone.utc),
                            None,  # updated_at
                            row["ended_at"],
                        ),
                    )
                    migrated_sessions += 1

                conn_pg.commit()
            except Exception as exc:
                conn_pg.rollback()
                raise
            finally:
                self._put_conn(conn_pg)

            # Migrate turns from session JSONL files
            hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
            sessions_dir = Path(hermes_home) / "sessions"

            if sessions_dir.exists():
                conn_pg = self._get_conn()
                try:
                    cur_pg = conn_pg.cursor()
                    for jsonl_file in sorted(sessions_dir.glob("*.jsonl")):
                        sess_id = jsonl_file.stem
                        # Check if already has turns
                        cur_pg.execute(
                            "SELECT COUNT(*) FROM pg_session_turns WHERE session_id = %s",
                            (sess_id,),
                        )
                        if cur_pg.fetchone()[0] > 0:
                            continue

                        try:
                            with open(jsonl_file) as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    msg = json.loads(line)
                                    role = msg.get("role", "unknown")
                                    content = json.dumps(msg.get("content", "")) if not isinstance(msg.get("content"), str) else msg.get("content", "")
                                    if isinstance(content, list):
                                        content = json.dumps(content)

                                    cur_pg.execute(
                                        """
                                        INSERT INTO pg_session_turns
                                            (session_id, turn_index, role, content, created_at)
                                        VALUES (%s, %s, %s, %s, %s)
                                        ON CONFLICT DO NOTHING
                                        """,
                                        (
                                            sess_id,
                                            msg.get("turn_index", migrated_turns),
                                            role,
                                            content[:100000] if content else "",  # cap at 100k chars
                                            msg.get("created_at", datetime.now(timezone.utc).isoformat()),
                                        ),
                                    )
                                    migrated_turns += 1

                            # Update turn count on session
                            cur_pg.execute(
                                """
                                UPDATE pg_sessions SET turn_count = (SELECT COUNT(*) FROM pg_session_turns WHERE session_id = %s)
                                WHERE id = %s
                                """,
                                (sess_id, sess_id),
                            )
                        except (json.JSONDecodeError, OSError) as exc:
                            logger.warning("pg-sessions: skipped %s: %s", jsonl_file.name, exc)

                    conn_pg.commit()
                except Exception as exc:
                    conn_pg.rollback()
                    raise
                finally:
                    self._put_conn(conn_pg)

            conn_sqlite.close()

            return json.dumps({
                "sessions_migrated": migrated_sessions,
                "turns_migrated": migrated_turns,
                "total_in_sqlite": total,
                "provider": self._provider,
                "message": f"Migrated {migrated_sessions} sessions and {migrated_turns} turns to PostgreSQL ({self._provider})",
            })

        except Exception as exc:
            return json.dumps({"error": str(exc)})