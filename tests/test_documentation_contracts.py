import json
import re
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
    package_lock = json.loads((REPO / "package-lock.json").read_text())
    version = (REPO / "VERSION").read_text().strip()
    landing = (REPO / "docs" / "index.html").read_text()
    landing_lower = landing.lower()

    assert package["name"] == "davellm-desktop"
    assert package["scripts"]["start"] == "electron ."
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        version,
    )
    assert package["version"] == version
    assert package_lock["version"] == version
    assert package_lock["packages"][""]["version"] == version

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
    project_spec = (REPO / "PROJECT_SPEC.md").read_text()
    integration = (REPO / "INTEGRATION.md").read_text()
    maintainer_contract = (REPO / "CLAUDE.md").read_text()

    for documented_path in (
        "[README.md](README.md)",
        "[PROJECT_SPEC.md](PROJECT_SPEC.md)",
        "[INTEGRATION.md](INTEGRATION.md)",
        "[CLAUDE.md](CLAUDE.md)",
        "[docs/OPERATIONS.md](docs/OPERATIONS.md)",
        "[DaveHarness boundary and versioning decision](docs/decisions/0001-daveharness-boundary-and-versioning.md)",
        "[DaveHarness implementation plan](docs/DAVEHARNESS_IMPLEMENTATION_PLAN.md)",
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

    for persisted_path in (
        "dave_conversations.json",
        "dave_projects.json",
        "dave_settings.json",
        "dave_project_context.db",
        "dave_vectors.db",
        "feedback.db",
        "performance.db",
        "cost_log.jsonl",
        "project_uploads/",
    ):
        assert persisted_path in operations
        assert persisted_path in project_spec

    for required_contract in (
        "## 4. Functional requirements",
        "## 7. System architecture",
        "## 9. Security and privacy requirements",
        "## 13. Quality and acceptance criteria",
        "## 15. Known limits and open decisions",
        "DAVE_ENABLE_TOOLS",
        "DAVE_ENABLE_SHELL_TOOL",
        "X-API-Key",
        "DaveHarness",
        "root `VERSION`",
    ):
        assert required_contract in project_spec

    for version_contract in (integration, operations, maintainer_contract):
        assert "`VERSION`" in version_contract

    for harness_contract in (project_spec, maintainer_contract):
        assert "DaveLLM consumes DaveHarness" in harness_contract

    assert "PLACEHOLDER_" not in operations
    assert "PLACEHOLDER_" not in project_spec


def test_daveharness_implementation_plan_is_complete_and_linked():
    plan = (REPO / "docs" / "DAVEHARNESS_IMPLEMENTATION_PLAN.md").read_text()
    project_spec = (REPO / "PROJECT_SPEC.md").read_text()
    action_ids = [
        int(match)
        for match in re.findall(r"^\| (\d+) \|", plan, flags=re.MULTILINE)
    ]

    assert action_ids == list(range(1, 61))
    for phase in range(10):
        assert f"### H{phase}:" in plan
    for required in (
        "**Baseline:** DaveLLM `2.1.0`, DaveHarness `0.1.0`",
        "**Current:** DaveLLM `2.1.0`, DaveHarness `0.7.0`",
        "**Target:** DaveHarness `1.0.0`",
        "RunSnapshot",
        "ApprovalDecision",
        "CancellationToken",
        "EventSink",
        "RunStore",
        "POST /tools/agent/runs",
        "Tools and shell remain default-off",
        "Blocked by runtime authorization",
    ):
        assert required in plan
    assert "(docs/DAVEHARNESS_IMPLEMENTATION_PLAN.md)" in project_spec


def test_daveharness_execution_semantics_are_documented_consistently():
    documents = {
        name: (REPO / path).read_text()
        for name, path in {
            "project_spec": Path("PROJECT_SPEC.md"),
            "maintainer": Path("CLAUDE.md"),
            "readme": Path("README.md"),
            "integration": Path("INTEGRATION.md"),
            "decision": Path(
                "docs/decisions/0001-daveharness-boundary-and-versioning.md"
            ),
            "plan": Path("docs/DAVEHARNESS_IMPLEMENTATION_PLAN.md"),
        }.items()
    }

    assert (REPO / "VERSION").read_text().strip() == "2.1.0"
    harness_version = (REPO / "daveharness" / "_version.py").read_text()
    assert re.search(r'^__version__ = "0\.7\.0"$', harness_version, re.MULTILINE)
    for content in documents.values():
        assert "0.2.0" in content
        assert "0.3.0" in content
        assert "0.4.0" in content
        assert "0.5.0" in content
        assert "0.6.0" in content
        assert "0.7.0" in content
    for name in ("project_spec", "maintainer", "readme", "integration", "decision"):
        assert "/tools/agent/resume" in documents[name]
        assert "deadline_abandoned" in documents[name]
        assert "300" in documents[name]
