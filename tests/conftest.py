"""Test harness: the plugin directory as package ``skill_court``, with minimal stand-ins for the
Hermes modules it imports, rooted in a per-test temporary Hermes home.

``register(ctx)`` is never run, so tests exercise the modules directly without a Hermes install.
"""

import re
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
_constants.profile_name_for_home = lambda path: "default" if path == _HOME["home"] else None
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


def _parse_frontmatter(content: str):
    """``agent.skill_utils.parse_frontmatter`` of Hermes 0.23, copied as is, with no YAML parser:
    every frontmatter takes Hermes' fallback for YAML it cannot parse (``key: value`` lines, the
    last of a repeated key winning), which is what Hermes does with a repeated ``name``."""
    content = content.removeprefix("\ufeff")
    end_match = re.search(r"\n---\s*\n", content[3:]) if content.startswith("---") else None
    if not end_match:
        return {}, content
    yaml_content = content[3: end_match.start() + 3]
    body = content[end_match.end() + 3:]
    frontmatter = {}
    for line in yaml_content.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()
    return frontmatter, body


_skill_utils = types.ModuleType("agent.skill_utils")
_skill_utils.parse_frontmatter = _parse_frontmatter
sys.modules.setdefault("agent", types.ModuleType("agent"))
sys.modules.setdefault("agent.skill_utils", _skill_utils)


@pytest.fixture(autouse=True)
def hermes_home(tmp_path):
    _HOME["home"] = tmp_path
    yield tmp_path
