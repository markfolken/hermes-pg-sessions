"""Tool schemas for pg-sessions — what the LLM sees."""

SESSIONS_QUERY = {
    "name": "sessions_query",
    "description": "Search sessions stored in PostgreSQL by date range, platform, user ID, or title. Returns session list with metadata.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search across session titles and IDs",
            },
            "platform": {
                "type": "string",
                "description": "Filter by platform (cli, telegram, discord, slack, etc.)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default 20, max 100)",
                "default": 20,
            },
            "offset": {
                "type": "integer",
                "description": "Pagination offset",
                "default": 0,
            },
            "since_days": {
                "type": "integer",
                "description": "Only sessions from the last N days",
            },
            "status": {
                "type": "string",
                "enum": ["active", "completed", "interrupted"],
                "description": "Filter by session status",
            },
        },
    },
}

SESSIONS_GET = {
    "name": "sessions_get",
    "description": "Get a full session transcript from PostgreSQL — all turns for a given session ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session ID to retrieve",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum turns to return (default 50, max 500)",
                "default": 50,
            },
            "offset": {
                "type": "integer",
                "description": "Pagination offset",
                "default": 0,
            },
        },
        "required": ["session_id"],
    },
}

SESSIONS_STATS = {
    "name": "sessions_stats",
    "description": "Get session and usage statistics from PostgreSQL — total sessions, turns, token estimates, active today.",
    "parameters": {
        "type": "object",
        "properties": {
            "since_days": {
                "type": "integer",
                "description": "Stats for the last N days (default 7, 0 = all time)",
                "default": 7,
            },
        },
    },
}

SESSIONS_MIGRATE = {
    "name": "sessions_migrate",
    "description": "Migrate local SQLite sessions (state.db, session files) into PostgreSQL. Idempotent — skips already-migrated sessions.",
    "parameters": {
        "type": "object",
        "properties": {
            "source_path": {
                "type": "string",
                "description": "Path to local state.db file (default: ~/.hermes/state.db)",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Preview what would be migrated without writing",
                "default": False,
            },
        },
    },
}