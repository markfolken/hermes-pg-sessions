"""Packaging tests — the plugin is discoverable and installable."""

from __future__ import annotations

import importlib.metadata as md
import pathlib

import pg_sessions


def test_package_imports() -> None:
    assert pg_sessions.__version__ == "1.0.0"


def test_register_is_callable() -> None:
    assert callable(pg_sessions.register)


def test_store_class_exported() -> None:
    assert pg_sessions.PGSessionStore is not None
    assert hasattr(pg_sessions.PGSessionStore, "connect")


def test_entry_point_registered() -> None:
    """The wheel must publish the hermes_agent.plugins entry point."""
    eps = md.entry_points()
    if hasattr(eps, "select"):
        group = eps.select(group="hermes_agent.plugins")
    else:  # pragma: no cover — Python < 3.10 compat
        group = eps.get("hermes_agent.plugins", [])

    names = {ep.name: ep.value for ep in group}
    assert "pg-sessions" in names, f"entry point missing; found {sorted(names)}"
    assert names["pg-sessions"] == "pg_sessions"


def test_plugin_yaml_ships_in_package() -> None:
    """force-include must place plugin.yaml next to __init__.py."""
    import pg_sessions as pkg

    manifest = pathlib.Path(pkg.__file__).parent / "plugin.yaml"
    assert manifest.is_file(), f"plugin.yaml not shipped at {manifest}"

    import yaml

    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert data["name"] == "pg-sessions"
    assert data["kind"] == "standalone"
    assert "PG_SESSIONS_URL" in data["requires_env"]
