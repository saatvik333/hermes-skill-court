"""Test harness: the plugin directory as package ``skill_court``, with minimal stand-ins for the two
Hermes modules its storage layer imports, rooted in a per-test temporary Hermes home.

``register(ctx)`` is never run, so tests exercise the modules directly without a Hermes install.
"""

import sqlite3
import sys
import types
from pathlib import Path

import pytest

_HOME: dict[str, Path] = {}

_package = types.ModuleType("skill_court")
_package.__path__ = [str(Path(__file__).resolve().parents[1] / "skill-court")]
sys.modules.setdefault("skill_court", _package)

_constants = types.ModuleType("hermes_constants")
_constants.get_hermes_home = lambda: _HOME["home"]
_constants.get_default_hermes_root = lambda: _HOME["home"]
sys.modules.setdefault("hermes_constants", _constants)


def _plugin_data_dir(name: str) -> Path:
    path = _HOME["home"] / "plugin-data" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plugin_db(name: str, filename: str = "data.db") -> sqlite3.Connection:
    conn = sqlite3.connect(_plugin_data_dir(name) / filename, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_storage = types.ModuleType("plugins.plugin_storage")
_storage.plugin_data_dir = _plugin_data_dir
_storage.plugin_db = _plugin_db
sys.modules.setdefault("plugins", types.ModuleType("plugins"))
sys.modules.setdefault("plugins.plugin_storage", _storage)


@pytest.fixture(autouse=True)
def hermes_home(tmp_path):
    _HOME["home"] = tmp_path
    yield tmp_path
