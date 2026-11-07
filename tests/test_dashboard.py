"""The Desktop backend (``dashboard/plugin_api.py``), with stand-ins for FastAPI and Pydantic."""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def plugin_api(monkeypatch):
    fastapi = types.ModuleType("fastapi")

    class APIRouter:
        def get(self, _path):
            return lambda handler: handler

        post = get

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    fastapi.APIRouter, fastapi.HTTPException = APIRouter, HTTPException
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = type("BaseModel", (), {"__init__": lambda self, **fields: self.__dict__.update(fields)})
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "pydantic", pydantic)
    path = Path(__file__).resolve().parents[1] / "skill-court" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("skill_court_plugin_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_desktop_applies_only_the_appeal_its_dialog_showed(plugin_api, monkeypatch):
    commands = []

    def run(argv, **_):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="Applied appeal #7", stderr="")

    monkeypatch.setattr(plugin_api.procs, "run", run)
    monkeypatch.setattr(plugin_api.procs, "hermes_binary", lambda: "hermes")
    body = plugin_api.ApplyBody(skill="csv-totals", model="claude-sonnet-5-5", appeal_id=7)
    assert plugin_api._apply(body)["ok"]
    assert commands == [["hermes", "-p", "default", "court", "appeal", "apply", "csv-totals",
                         "--model", "claude-sonnet-5-5", "--appeal", "7", "--yes"]]
