"""Test helpers: a fake Hermes ctx, and a DataSource fed from fixture CSVs (no network)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Import the plugin as a package named "afriradar", the way Hermes namespaces it.
if "afriradar" not in sys.modules:
    spec = importlib.util.spec_from_file_location("afriradar", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["afriradar"] = module
    spec.loader.exec_module(module)

from afriradar import data, tools  # noqa: E402


def fixture_fetcher(calls: list | None = None):
    def fetch(url: str, headers: dict):
        name = url.rsplit("/", 1)[-1]
        if calls is not None:
            calls.append((name, dict(headers)))
        path = FIXTURES / name
        if not path.exists():
            return 200, {}, b"week_ended,category,fund\n"
        return 200, {"ETag": f'"{name}"'}, path.read_bytes()
    return fetch


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        import json
        self.data[key] = json.loads(json.dumps(value))  # same round trip as Hermes' JSON state


class FakeCtx:
    def __init__(self, config=None):
        self.config = config or {}
        self.state = FakeState()
        self.tools, self.commands, self.skills, self.blueprints, self.hooks = {}, {}, {}, {}, {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kw}

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_hook(self, name, callback, **kw):
        self.hooks[name] = callback

    def register_skill(self, name, path):
        self.skills[name] = path

    def register_automation_blueprint(self, key, **kw):
        self.blueprints[key] = kw


@pytest.fixture
def runtime(tmp_path):
    source = data.DataSource("https://www.afriradar.com/data/", tmp_path / "cache", fetcher=fixture_fetcher())
    state = FakeState()
    return tools.Runtime(source, tools.Store(state.get, state.set), alert_bps=50)
