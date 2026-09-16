"""Tool handlers for pg-sessions plugin tools.

Each handler receives (args, **kwargs) and returns a JSON string.
"""

import json
import logging

logger = logging.getLogger("plugins.pg-sessions.tools")


def handle_sessions_query(store, args, **kwargs):
    """Search sessions in PostgreSQL."""
    query_text = args.get("query", "")
    platform = args.get("platform", "")
    limit = min(int(args.get("limit", 20)), 100)
    offset = int(args.get("offset", 0))
    since_days = int(args.get("since_days", 0))
    status = args.get("status", "")

    if not store or not store._connected:
        return json.dumps({"error": "pg-sessions not connected. Set PG_SESSIONS_URL and restart."})

    result = store.query_sessions(
        query_text=query_text,
        platform=platform,
        limit=limit,
        offset=offset,
        since_days=since_days,
        status=status,
    )
    return result


def handle_sessions_get(store, args, **kwargs):
    """Get a session with its turns."""
    session_id = args.get("session_id", "")
    limit = min(int(args.get("limit", 50)), 500)
    offset = int(args.get("offset", 0))

    if not session_id:
        return json.dumps({"error": "session_id is required"})
    if not store or not store._connected:
        return json.dumps({"error": "pg-sessions not connected. Set PG_SESSIONS_URL and restart."})

    result = store.get_session(session_id, limit=limit, offset=offset)
    return result


def handle_sessions_stats(store, args, **kwargs):
    """Get session statistics."""
    since_days = int(args.get("since_days", 7))

    if not store or not store._connected:
        return json.dumps({"error": "pg-sessions not connected. Set PG_SESSIONS_URL and restart."})

    result = store.get_stats(since_days=since_days)
    return result


def handle_sessions_migrate(store, args, **kwargs):
    """Migrate from local SQLite to PostgreSQL."""
    source_path = args.get("source_path", "~/.hermes/state.db")
    dry_run = bool(args.get("dry_run", False))

    if not store or not store._connected:
        return json.dumps({"error": "pg-sessions not connected. Set PG_SESSIONS_URL and restart."})

    result = store.migrate_from_sqlite(source_path, dry_run=dry_run)
    return result