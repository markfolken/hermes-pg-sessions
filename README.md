# hermes-pg-sessions

[![CI](https://github.com/markfolken/hermes-pg-sessions/actions/workflows/ci.yml/badge.svg)](https://github.com/markfolken/hermes-pg-sessions/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

PostgreSQL session storage for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes keeps sessions in a local SQLite file (`~/.hermes/state.db`) plus per-session
JSONL transcripts. That is fine on one machine and painful everywhere else — no
shared history across VPSs, no queryable archive, no backups that survive the box.

This plugin moves session history and conversation turns into PostgreSQL, so any
Hermes instance pointed at the same database sees the same history.

**Compatible with:** Neon · Supabase · Aiven · DigitalOcean · AWS RDS · any PostgreSQL 12+

---

## Install

```bash
pip install hermes-pg-sessions
hermes plugins enable pg-sessions
```

Or from source:

```bash
git clone https://github.com/markfolken/hermes-pg-sessions
cd hermes-pg-sessions
pip install -e ".[dev]"
hermes plugins enable pg-sessions
```

## Configure

Set `PG_SESSIONS_URL` in `$HERMES_HOME/.env` (usually `~/.hermes/.env`):

```bash
# Neon
PG_SESSIONS_URL=postgresql://user:pass@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require

# Neon — pooled endpoint (recommended; serverless-friendly)
PG_SESSIONS_URL=postgresql://user:pass@ep-xxxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require

# Supabase — transaction pooler, port 6543
PG_SESSIONS_URL=postgresql://postgres:pass@db.xxxx.supabase.co:6543/postgres?sslmode=require

# Standard / self-hosted
PG_SESSIONS_URL=postgresql://user:pass@host:5432/dbname
```

Tables are created automatically on first connect. No migrations to run by hand.

Verify:

```bash
hermes pg-sessions status
hermes pg-sessions stats --days 30
```

## CLI

```
hermes pg-sessions status              # connection + detected provider
hermes pg-sessions stats [--days N]    # session/turn/token counts
hermes pg-sessions migrate [--source PATH] [--dry-run]
hermes pg-sessions connect --dsn URL   # test a connection string
```

## Tools

The model gets four tools, visible only once `PG_SESSIONS_URL` is set:

| Tool | Purpose |
|------|---------|
| `sessions_query` | Search by free text (session title **and** turn content), platform, status, date range |
| `sessions_get` | Full transcript for one session ID |
| `sessions_stats` | Session, turn, and token aggregates by platform and status |
| `sessions_migrate` | Import local SQLite history into PostgreSQL (idempotent) |

## Migrating existing history

```bash
hermes pg-sessions migrate --source ~/.hermes/state.db --dry-run   # preview
hermes pg-sessions migrate --source ~/.hermes/state.db             # execute
```

Reads the `sessions` table from `state.db` and the per-session JSONL transcripts
from `$HERMES_HOME/sessions/`. Safe to re-run: already-migrated sessions are
skipped by primary-key conflict.

## Schema

Two tables, created on first connect. No extensions required — works on any
managed PostgreSQL that forbids `CREATE EXTENSION`.

```sql
CREATE TABLE pg_sessions (
    id            TEXT PRIMARY KEY,
    title         TEXT DEFAULT '',
    platform      TEXT DEFAULT '',
    user_id       TEXT DEFAULT '',
    model         TEXT DEFAULT '',
    provider      TEXT DEFAULT '',
    status        TEXT DEFAULT 'active',   -- active | completed | interrupted
    turn_count    INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    ended_at      TIMESTAMPTZ,
    metadata      JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE pg_session_turns (
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
```

`ON DELETE CASCADE` means deleting a session removes its turns — useful for
retention policies driven by `created_at`.

## Design notes

### Cloud provider quirks, handled

| Provider | Quirk | Handling |
|----------|-------|----------|
| **Neon** | Compute auto-suspends; first query after idle costs 500 ms–2 s | 3 connection retries with 0.5 s / 1.0 s / 1.5 s backoff |
| **Neon / Supabase** | PGBouncer in transaction mode rejects session state | `autocommit=True` when pooled; no `SET`, no `LISTEN/NOTIFY`, no session-scoped prepared statements |
| **Supabase** | RLS on `public` | Tables owned by the connecting role; no policy bypass needed |
| **All managed** | `CREATE EXTENSION` often blocked | Python-generated IDs, vanilla SQL only |
| **All hosted** | TLS mandatory | `sslmode=require` auto-appended for known providers |

Provider is auto-detected from the DSN and reported by `hermes pg-sessions status`.

### Failure behaviour

The agent loop must never break because a database is slow or down.

- **Circuit breaker** — 5 consecutive failures opens the breaker for 120 s. During
  that window every write is dropped and every read returns a clear error, with
  zero latency cost. Client errors (4xx-class) do not trip it; only connection and
  server failures do.
- **Non-blocking writes** — turns are queued and flushed by a daemon thread, so
  nothing on the agent's critical path waits on the network.
- **Flush barriers on read** — `query_sessions`, `get_session`, and `get_stats`
  flush pending writes first, so a read never misses a turn that just happened.
- **All hooks swallow exceptions** — a broken backend degrades the plugin, never
  the conversation.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v                       # unit tests, no database needed
```

Integration tests need a real PostgreSQL:

```bash
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=hermes_sessions postgres:16
export PG_SESSIONS_URL_TEST="postgresql://postgres:test@localhost:5432/hermes_sessions"
pytest tests/ -v                       # integration tests now run too
```

## Licence

MIT — see [LICENSE](LICENSE).
