import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_public_documentation_matches_the_desktop_runtime_contract():
    package = json.loads((REPO / "package.json").read_text())
    landing = (REPO / "docs" / "index.html").read_text()
    landing_lower = landing.lower()

    assert package["name"] == "davellm-desktop"
    assert package["scripts"]["start"] == "electron ."

    for required in (
        "npm start",
        "DAVE_API_KEY",
        "DAVE_NODES",
        "http://127.0.0.1:8000/",
        "Electron",
        "FastAPI",
        "Ollama",
    ):
        assert required in landing

    for obsolete in (
        "npx davellm start",
        "davellm start --model",
        "localhost:3000",
        "/ui",
        "no api keys",
        "drop-in replacement",
        "/v1/chat/completions",
        "gguf",
    ):
        assert obsolete not in landing_lower


def test_documentation_map_and_operations_runbook_preserve_boundaries():
    readme = (REPO / "README.md").read_text()
    operations = (REPO / "docs" / "OPERATIONS.md").read_text()

    for documented_path in (
        "[README.md](README.md)",
        "[INTEGRATION.md](INTEGRATION.md)",
        "[CLAUDE.md](CLAUDE.md)",
        "[docs/OPERATIONS.md](docs/OPERATIONS.md)",
        "[dave-llm-feature-analysis-2026-03-25.md](dave-llm-feature-analysis-2026-03-25.md)",
        "[Public landing page](https://davehomeassist.github.io/DaveLLM/)",
    ):
        assert documented_path in readme

    for required in (
        "DAVE_API_KEY",
        "DAVE_NODES",
        "DAVE_DATA_DIR",
        "GET /health",
        "GET /nodes",
        "GET /nodes/{node_id}/models",
        "http://127.0.0.1:8000/",
        "X-API-Key",
    ):
        assert required in operations

    assert "PLACEHOLDER_" not in operations
