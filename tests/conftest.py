import importlib
import json
import sys

import pytest
from fastapi.testclient import TestClient

from hostile_fs import build_hostile_tree
from hostile_git import build_hostile_git


TEST_API_KEY = "test-only-api-key"
TEST_NODE_URL = "http://ollama.test:11434"
TEST_NODE = {"id": "node-test", "name": "Test Ollama", "url": TEST_NODE_URL}


@pytest.fixture
def hostile_tree(tmp_path):
    """Planted secrets, escaping symlinks, and awkward files; see tests/hostile_fs.py."""
    return build_hostile_tree(tmp_path / "hostile")


@pytest.fixture
def hostile_git(tmp_path):
    """Hostile Git repositories and planted programs; see tests/hostile_git.py.

    Every test that uses it also proves that no planted program ever ran.
    """
    tree = build_hostile_git(tmp_path / "hostile-git")
    yield tree
    assert tree.fired() == [], f"a read-only Git tool ran planted programs: {tree.fired()}"


@pytest.fixture
def router_factory(monkeypatch, tmp_path):
    clients = []

    def load(*, api_key=TEST_API_KEY, tools=False, tool_roots=None, shell=False):
        data_dir = tmp_path / f"data-{len(clients)}"
        monkeypatch.setenv("DAVE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("DAVE_NODES", json.dumps([TEST_NODE]))
        if api_key is None:
            monkeypatch.delenv("DAVE_API_KEY", raising=False)
        else:
            monkeypatch.setenv("DAVE_API_KEY", api_key)
        monkeypatch.setenv("DAVE_ENABLE_TOOLS", "true" if tools else "false")
        monkeypatch.setenv("DAVE_ENABLE_SHELL_TOOL", "true" if shell else "false")
        monkeypatch.setenv("DAVE_TOOL_ROOTS", json.dumps(tool_roots or []))

        sys.modules.pop("app", None)
        router = importlib.import_module("app")
        client = TestClient(router.app)
        clients.append(client)
        return router, client, data_dir

    yield load

    for client in clients:
        client.close()
    sys.modules.pop("app", None)
