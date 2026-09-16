"""CLI subcommand for pg-sessions.

Adds `hermes pg-sessions <action>` for managing PostgreSQL session storage.
"""

import argparse
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("plugins.pg-sessions.cli")

# Lazy import of store — avoids psycopg2 dependency at plugin discovery time
_store = None


def _get_store():
    global _store
    if _store is not None:
        return _store

    dsn = os.environ.get("PG_SESSIONS_URL", "")
    if not dsn:
        from .store import PGSessionStore
        _store = PGSessionStore()
        return _store

    from .store import PGSessionStore
    _store = PGSessionStore()
    _store.connect(dsn)
    return _store


def setup_cli(parser: argparse.ArgumentParser) -> None:
    """Configure argparse for `hermes pg-sessions`."""
    parser.add_argument(
        "action",
        nargs="?",
        choices=["status", "stats", "migrate", "connect"],
        default="status",
        help="Action to perform",
    )
    parser.add_argument(
        "--dsn",
        help="PostgreSQL connection string (overrides PG_SESSIONS_URL env var)",
        default="",
    )
    parser.add_argument(
        "--source",
        help="Path to local state.db for migration (default: ~/.hermes/state.db)",
        default="~/.hermes/state.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration without writing",
        default=False,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Stats window in days (default: 7, 0 = all time)",
    )


def handler_fn(args) -> int:
    """Handle `hermes pg-sessions <action>`.

    Args is argparse.Namespace — use getattr(), never args.get().
    """
    action = str(getattr(args, "action", "") or "status").strip().lower()
    dsn = str(getattr(args, "dsn", "") or "")
    source = str(getattr(args, "source", "~/.hermes/state.db") or "~/.hermes/state.db")
    dry_run = bool(getattr(args, "dry_run", False))
    days = int(getattr(args, "days", 7))

    try:
        if action == "connect":
            dsn = dsn or os.environ.get("PG_SESSIONS_URL", "")
            if not dsn:
                print("ERROR: No connection string. Set PG_SESSIONS_URL or pass --dsn")
                return 1

            store = _get_store() if _store else None
            if store is None:
                from .store import PGSessionStore
                store = PGSessionStore()

            print(f"Connecting to PostgreSQL...")
            store.connect(dsn)
            print(f"✓ Connected (provider={store._provider}, pooled={store._is_pooled})")
            print()
            print("Connection string examples by provider:")
            print("  Neon:      postgresql://user:pass@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require")
            print("  Neon+pool: postgresql://user:pass@ep-xxxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require")
            print("  Supabase:  postgresql://postgres:pass@db.xxxx.supabase.co:6543/postgres?sslmode=require")
            print("  Standard:  postgresql://user:pass@host:5432/dbname")
            return 0

        elif action == "status":
            store = _get_store()
            if store._connected:
                print(f"pg-sessions: ✓ Connected")
                print(f"  Provider: {store._provider}")
                print(f"  Pooled:   {store._is_pooled}")
            else:
                print("pg-sessions: ✗ Not connected")
                print("  Set PG_SESSIONS_URL or run: hermes pg-sessions connect --dsn <url>")
            return 0

        elif action == "stats":
            store = _get_store()
            if not store._connected:
                print("ERROR: Not connected. Run 'hermes pg-sessions connect' first.")
                return 1
            result = json.loads(store.get_stats(since_days=days))
            if "error" in result:
                print(f"ERROR: {result['error']}")
                return 1
            print(f"pg-sessions stats (last {result['since_days']}d):")
            print(f"  Sessions:         {result['total_sessions']}")
            print(f"  Turns:            {result['total_turns']}")
            print(f"  Tokens:           {result['total_tokens']}")
            print(f"  Avg turns/session: {result['avg_turns_per_session']}")
            print(f"  Active now:       {result['active_sessions']}")
            print(f"  By status:        {result['by_status']}")
            print(f"  By platform:      {result['by_platform']}")
            return 0

        elif action == "migrate":
            store = _get_store()
            if not store._connected:
                print("ERROR: Not connected. Run 'hermes pg-sessions connect' first.")
                return 1
            result = json.loads(store.migrate_from_sqlite(source, dry_run=dry_run))
            if "error" in result:
                print(f"ERROR: {result['error']}")
                return 1
            print(json.dumps(result, indent=2))
            return 0

        return 0

    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1