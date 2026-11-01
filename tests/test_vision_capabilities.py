"""Backlog: "Fix vision capability detection for Ollama models".

/api/tags carries no capabilities, so the router guessed vision support from
model names and called qwen3.5:4b and gemma3:27b text-only. Ollama's
POST /api/show reports "capabilities"; the inventory now uses it first.
"""

import json

import httpx
import respx

from conftest import TEST_NODE_URL
from test_api_contracts import AUTH

TAGS = ["qwen3.5:4b", "gemma3:27b", "vision-helper:latest", "flaky:latest", "old-node:latest"]
SHOW = {
    "qwen3.5:4b": httpx.Response(200, json={"capabilities": ["completion", "vision", "tools", "thinking"]}),
    "gemma3:27b": httpx.Response(200, json={"capabilities": ["completion", "vision"]}),
    # The alias says vision; Ollama says text only.
    "vision-helper:latest": httpx.Response(200, json={"capabilities": ["completion"]}),
    "flaky:latest": httpx.Response(500, text="runner busy"),
    "old-node:latest": httpx.Response(200, json={"modelfile": "FROM x"}),  # predates capabilities
}


def mock_node(mock, show=SHOW):
    mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(
        200, json={"models": [{"name": name, "model": name} for name in TAGS]}))

    def answer(request):
        return show[json.loads(request.content)["model"]]

    return mock.post(f"{TEST_NODE_URL}/api/show").mock(side_effect=answer)


def inventory(client):
    response = client.get("/nodes/node-test/models", headers=AUTH)
    assert response.status_code == 200
    return {model["id"]: (model["vision"], model["capabilities_source"]) for model in response.json()["models"]}


def test_ollama_capabilities_decide_vision_support_over_model_names(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_node(mock)
        models = inventory(client)
    assert models == {
        "qwen3.5:4b": (True, "ollama"),
        "gemma3:27b": (True, "ollama"),
        "vision-helper:latest": (False, "ollama"),
        # Lookup failures keep the name guess and say so, and never break the inventory.
        "flaky:latest": (False, "name"),
        "old-node:latest": (False, "name"),
    }


def test_a_timed_out_lookup_keeps_the_inventory_and_the_name_guess(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(
            200, json={"models": [{"name": "llava-vision:7b"}, {"name": "plain:1b"}]}))
        mock.post(f"{TEST_NODE_URL}/api/show").mock(side_effect=httpx.ReadTimeout("slow"))
        models = inventory(client)
    assert models == {"llava-vision:7b": (True, "name"), "plain:1b": (False, "name")}


def test_lookups_are_cached_and_bounded(router_factory, monkeypatch):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        show = mock_node(mock)
        inventory(client)
        first = show.call_count
        inventory(client)
        # Only failed lookups are retried on the next refresh.
        assert show.call_count - first == 2
    assert first == len(TAGS)

    router.SHOW_CAPABILITIES_CACHE.clear()
    monkeypatch.setattr(router, "SHOW_LOOKUP_LIMIT", 2)
    with respx.mock(assert_all_called=True) as mock:
        show = mock_node(mock)
        models = inventory(client)
    assert show.call_count == 2
    assert [source for _, source in models.values()][2:] == ["name", "name", "name"]
