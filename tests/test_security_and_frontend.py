import os
import socket
import subprocess
from pathlib import Path

import httpx
import respx

from conftest import TEST_API_KEY


AUTH = {"X-API-Key": TEST_API_KEY}


def test_tools_are_disabled_by_default(router_factory):
    _, client, _ = router_factory()
    assert client.get("/tools", headers=AUTH).status_code == 403
    assert client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "system.info", "params": {}},
    ).status_code == 403


def test_tool_roots_apply_to_read_write_append_and_shell_stays_off(router_factory, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "allowed-sibling" / "blocked.txt"
    _, client, _ = router_factory(tools=True, tool_roots=[str(allowed)])
    listed = client.get("/tools", headers=AUTH).json()["tools"]
    assert "file.read" in listed
    assert "shell.exec" not in listed

    target = allowed / "note.txt"
    write = client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "file.write", "params": {"path": str(target), "content": "one"}},
    ).json()
    assert write["status"] == "success"
    append = client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "file.append", "params": {"path": str(target), "content": " two"}},
    ).json()
    assert append["status"] == "success"
    read = client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "file.read", "params": {"path": str(target)}},
    ).json()
    assert read["result"] == "one two"

    denied = client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "file.write", "params": {"path": str(outside), "content": "no"}},
    ).json()
    assert denied["status"] == "error"
    assert "outside DAVE_TOOL_ROOTS" in denied["error"]

    shell = client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "shell.exec", "params": {"command": "pwd"}},
    ).json()
    assert shell["status"] == "error"
    assert "disabled" in shell["error"]


def test_web_fetch_rejects_loopback(router_factory):
    _, client, _ = router_factory(tools=True)
    response = client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "web.fetch", "params": {"url": "http://127.0.0.1/private"}},
    ).json()
    assert response["status"] == "error"
    assert "not public" in response["error"]


def test_web_fetch_validates_redirects_and_response_size(router_factory, monkeypatch):
    router, client, _ = router_factory(tools=True)

    def public_dns(host, *_args, **_kwargs):
        address = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    monkeypatch.setattr(router.socket, "getaddrinfo", public_dns)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://public.test/redirect").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        )
        redirected = client.post(
            "/tools/execute",
            headers=AUTH,
            json={"tool": "web.fetch", "params": {"url": "http://public.test/redirect"}},
        ).json()
        assert redirected["status"] == "error"
        assert "not public" in redirected["error"]

        mock.get("http://public.test/large").mock(
            return_value=httpx.Response(200, content=b"x" * (router.MAX_WEB_FETCH_BYTES + 1))
        )
        oversized = client.post(
            "/tools/execute",
            headers=AUTH,
            json={"tool": "web.fetch", "params": {"url": "http://public.test/large"}},
        ).json()
        assert oversized["status"] == "error"
        assert "byte limit" in oversized["error"]


def test_data_dir_contains_every_persistence_artifact(router_factory):
    router, _, data_dir = router_factory()
    paths = {
        router.DATA_FILE,
        router.PROJECTS_FILE,
        router.VECTOR_DB,
        router.FEEDBACK_DB,
        router.PERFORMANCE_DB,
        router.COST_LOG,
    }
    assert {path.name for path in paths} == {
        "dave_conversations.json",
        "dave_projects.json",
        "dave_vectors.db",
        "feedback.db",
        "performance.db",
        "cost_log.jsonl",
    }
    assert all(path.parent == data_dir.resolve() for path in paths)


def test_unset_data_dir_preserves_current_directory_default(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop("DAVE_DATA_DIR", None)
    env["DAVE_API_KEY"] = "subprocess-test-key"
    env["DAVE_NODES"] = "[]"
    env["PYTHONPATH"] = str(repo)
    result = subprocess.run(
        [str(repo / "venv" / "bin" / "python"), "-c", "import app; print(app.BASE_DIR)"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip().splitlines()[-1] == str(tmp_path.resolve())


def test_displayed_prompt_contract_and_renderer_security():
    repo = Path(__file__).resolve().parents[1]
    contract = repo / "static" / "prompt-contract.js"
    script = (
        f"const p=require({str(contract)!r});"
        "const value=p.buildDisplayedPrompt('Review this','\\n\\n[Attached file: note.txt]\\nBody',true);"
        "if(value !== '[SUPPORT] Review this\\n\\n[Attached file: note.txt]\\nBody') process.exit(1);"
    )
    subprocess.run(["node", "-e", script], check=True)

    app_source = (repo / "static" / "app.js").read_text()
    monitoring_source = (repo / "static" / "monitoring.html").read_text()
    preload_source = (repo / "desktop" / "preload.js").read_text()
    main_source = (repo / "desktop" / "main.js").read_text()

    assert "prompt: effectivePrompt" in app_source
    assert "content: effectivePrompt" in app_source
    assert "showRelevantMemories(effectivePrompt)" in app_source
    assert "if (data.error) throw new Error(data.error)" in app_source
    assert "messages: []," in app_source
    assert 'system_prompt: data.system_prompt || ""' in app_source
    assert 'messages: [{ role: "system"' not in app_source
    assert "innerHTML" not in app_source
    assert "innerHTML" not in monitoring_source
    assert "localStorage.getItem(\"dave_api_key\")" not in app_source + monitoring_source
    assert "DAVE_API_KEY" not in preload_source
    assert 'details.requestHeaders["X-API-Key"] = apiKey' in main_source
    assert main_source.index("await waitForBackend(child)") < main_source.index("createWindow();")
