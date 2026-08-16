import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class DocumentationAttributeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.copy_payload = None
        self.copy_handler = None

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        attributes = dict(attrs)
        if "data-copy" in attributes:
            self.copy_payload = attributes["data-copy"]
            self.copy_handler = attributes.get("onclick")


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

    assert "nav { flex-direction: column; align-items: flex-start; gap: 14px; }" in landing
    assert ".nav-links { width: 100%; flex-wrap: wrap; gap: 10px 18px; }" in landing
    assert "data-copy=\"export DAVE_API_KEY=" in landing
    assert "onclick=\"copyToClipboard(this, this.dataset.copy)\"" in landing
    assert "const orig = el.innerHTML;" in landing
    assert "el.innerHTML = orig;" in landing

    parser = DocumentationAttributeParser()
    parser.feed(landing)
    parser.close()
    assert parser.copy_payload == (
        "export DAVE_API_KEY='<local-secret>'\n"
        "export DAVE_NODES='<configured-node-json>'\n"
        "npm start"
    )
    assert parser.copy_handler == "copyToClipboard(this, this.dataset.copy)"
    subprocess.run(
        ["node", "-e", f"new Function({parser.copy_handler!r})"],
        check=True,
    )


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
