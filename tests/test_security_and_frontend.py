import os
import socket
import subprocess
import sys
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
    assert write["termination"] == "completed"
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
    assert denied["termination"] == "error"
    assert "outside DAVE_TOOL_ROOTS" in denied["error"]

    shell = client.post(
        "/tools/execute",
        headers=AUTH,
        json={"tool": "shell.exec", "params": {"command": "pwd"}},
    ).json()
    assert shell["status"] == "error"
    assert shell["termination"] == "denied"
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
        mock.get("http://93.184.216.34/redirect").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        )
        redirected = client.post(
            "/tools/execute",
            headers=AUTH,
            json={"tool": "web.fetch", "params": {"url": "http://public.test/redirect"}},
        ).json()
        assert redirected["status"] == "error"
        assert "not public" in redirected["error"]

        mock.get("http://93.184.216.34/large").mock(
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
        router.SETTINGS_FILE,
        router.VECTOR_DB,
        router.FEEDBACK_DB,
        router.PERFORMANCE_DB,
        router.PROJECT_CONTEXT_DB,
        router.COST_LOG,
    }
    assert {path.name for path in paths} == {
        "dave_conversations.json",
        "dave_projects.json",
        "dave_settings.json",
        "dave_vectors.db",
        "feedback.db",
        "performance.db",
        "dave_project_context.db",
        "cost_log.jsonl",
    }
    assert all(path.parent == data_dir.resolve() for path in paths)


def test_audio_transcription_uses_configured_runtime_and_cleans_temporary_files(
    router_factory,
    monkeypatch,
    tmp_path,
):
    router, client, data_dir = router_factory()
    whisper_bin = tmp_path / "whisper-cli"
    whisper_model = tmp_path / "ggml-tiny.en.bin"
    ffmpeg_bin = tmp_path / "ffmpeg"
    for runtime_file in (whisper_bin, whisper_model, ffmpeg_bin):
        runtime_file.write_bytes(b"test runtime")
    whisper_bin.chmod(0o755)
    ffmpeg_bin.chmod(0o755)
    router.WHISPER_BIN = whisper_bin
    router.WHISPER_MODEL = whisper_model
    router.FFMPEG_BIN = str(ffmpeg_bin)
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[0] == str(ffmpeg_bin):
            Path(command[-1]).write_bytes(b"wav")
        else:
            output_prefix = command[command.index("-of") + 1]
            Path(f"{output_prefix}.txt").write_text("Dictation verified.")

    monkeypatch.setattr(router.subprocess, "run", fake_run)
    response = client.post(
        "/audio/transcribe",
        headers=AUTH,
        files={"file": ("dictation.webm", b"recorded audio", "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "Dictation verified."
    assert [command[0] for command in commands] == [str(ffmpeg_bin), str(whisper_bin)]
    assert not list(data_dir.glob("tmp_audio_*"))


def _repo_python(repo):
    if os.name == "nt":
        venv_python = repo / "venv" / "Scripts" / "python.exe"
    else:
        venv_python = repo / "venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def test_unset_data_dir_preserves_current_directory_default(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop("DAVE_DATA_DIR", None)
    env["DAVE_API_KEY"] = "subprocess-test-key"
    env["DAVE_NODES"] = "[]"
    env["PYTHONPATH"] = str(repo)
    result = subprocess.run(
        [_repo_python(repo), "-c", "import app; print(app.BASE_DIR)"],
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
    index_source = (repo / "static" / "index.html").read_text()
    style_source = (repo / "static" / "style.css").read_text()
    gsap_source = (repo / "static" / "vendor" / "gsap" / "gsap.min.js").read_text()
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
    # The router is the only conversation and feedback store. The renderer must
    # not mirror prompt or response content into persistent browser storage or
    # restore a browser copy when the router is unreachable.
    assert "saveAllConversations" not in app_source
    assert "loadAllConversations" not in app_source
    assert "localStorage.setItem(LOCAL_STORAGE_KEY" not in app_source
    assert 'localStorage.setItem("dave_convos"' not in app_source
    assert 'localStorage.setItem("dave_feedback"' not in app_source
    assert 'LEGACY_CONTENT_STORAGE_KEYS = ["dave_convos", "dave_feedback"]' in app_source
    assert "purgeLegacyContentStorage();" in app_source
    assert "DAVE_API_KEY" not in preload_source
    assert 'details.requestHeaders["X-API-Key"] = apiKey' in main_source
    assert main_source.index("await waitForBackend(child)") < main_source.index("createWindow();")
    assert all(
        element_id in index_source
        for element_id in (
            'id="topbarStatusDot"',
            'id="topbarNodeValue"',
            'id="topbarStatusValue"',
            'id="topbarModelValue"',
        )
    )
    assert 'class="topbar-status-dot status-unknown"' in index_source
    assert 'class="skip-link" href="#chatView">Skip to main content' in index_source
    assert index_source.count("<main ") == 4
    for view, heading in (("chat", "chatHeading"), ("projects", "projectsHeading"),
                          ("cluster", "runtimeHeading"), ("settings", "settingsHeading")):
        assert f'<main id="{view}View"' in index_source
        assert f'aria-labelledby="{heading}" tabindex="-1"' in index_source
    assert '<section id="chatPanel"' in index_source
    assert 'id="chatPanel"' in index_source and 'tabindex="-1"' in index_source
    assert 'const nodeStatus = ["online", "offline"].includes(rawNodeStatus)' in app_source
    assert "@media (prefers-reduced-motion: reduce)" in style_source
    assert 'id="instructionsDialog"' in index_source
    assert 'id="globalInstructions"' in index_source
    assert 'id="projectInstructions"' in index_source
    assert 'id="sessionInstructions"' in index_source
    assert 'id="effectiveInstructions"' in index_source
    assert "new Uint8Array(await blob.arrayBuffer())" in app_source
    assert 'form.append("file", uploadBlob, "dictation.webm")' in app_source
    assert 'id="notepadPanel"' in index_source
    assert 'id="notepadInput"' in index_source
    assert 'id="projectHomeDialog"' in index_source
    assert 'id="projectHomeInstructions"' in index_source
    assert 'id="projectFilesList"' in index_source
    assert 'id="projectArtifactsList"' in index_source
    assert 'id="brainPinned"' in index_source
    assert 'id="brainActive"' in index_source
    assert 'id="brainRecent"' in index_source
    assert 'id="previewProjectContext"' in index_source
    assert 'id="projectContextPreviewOutput"' in index_source
    assert "async function openProjectHomepage()" in app_source
    assert "/homepage`)" in app_source
    assert "/context-preview`)" in app_source
    assert "/brain/compact`)" in app_source
    assert "/project`)" in app_source
    assert "vendor/lucide/lucide.svg#image" in index_source
    assert "vendor/lucide/lucide.svg#file-audio" in index_source
    assert "vendor/lucide/lucide.svg#captions" in index_source
    assert "vendor/lucide/lucide.svg#mic" in index_source
    assert "vendor/lucide/lucide.svg#paperclip" in index_source
    assert '<script src="vendor/gsap/gsap.min.js"></script>' in index_source
    assert "GSAP 3.15.0" in gsap_source[:200]
    assert "window.gsap.matchMedia()" in app_source
    assert '"(prefers-reduced-motion: no-preference)"' in app_source
    assert '"(prefers-reduced-motion: reduce)"' in app_source
    assert all(
        glyph not in index_source + app_source
        for glyph in ("📷", "🎤", "🗣️", "✍️", "🎙️", "⏹️", "📎")
    )
    assert 'aria-controls="notepadPanel"' in index_source
    assert "navigator.clipboard?.writeText" in app_source
    assert "rawMessageText(message)" in app_source
    assert 'label.textContent = "Copied"' in app_source
    assert "}, 2000);" in app_source
    assert 'copyButton.append(uiElement("span", "message-action-label", "Copy"));' in app_source
    assert 'setLucideIcon(copyButton, "copy");' in app_source
    assert '<symbol id="copy"' in (repo / "static" / "vendor" / "lucide" / "lucide.svg").read_text()
    assert 'noteButton.textContent = "Add to notepad"' in app_source
    assert "/instructions/session" in app_source
    assert "/notepad`" in app_source
    assert "async function flushProjectNotepadSave()" in app_source
    assert "await flushProjectNotepadSave();" in app_source
    assert "notepadSaveTimer = setTimeout(() =>" in app_source
    assert "@media (hover: none), (pointer: coarse)" in style_source
    assert ".message:hover .message-actions" in style_source


def test_stream_notice_replaces_the_blank_bubble_and_is_not_reloaded_away():
    """A tool-call-only reply arrives as a `notice` event; the renderer shows it as plain text."""
    app_source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    error_check = app_source.index("if (data.error) throw new Error(data.error);")
    notice = app_source.index("if (data.notice) {")
    token = app_source.index("if (data.token && !data.done) {")
    assert error_check < notice < token
    handler = app_source[notice:token]
    assert 'streamingMsg.role = "system";' in handler
    assert "streamingMsg.content = String(data.notice);" in handler
    assert "streamingMsg.isStreaming = false;" in handler
    assert "sawNotice = true;" in handler
    # The router saved no reply, so reloading history on done would erase the notice.
    assert 'if (!sawNotice && convo.title === "New Conversation") {' in app_source
    # Run tools builds its transcript from user and assistant turns only, never the notice.
    assert '.filter((item) => ["user", "assistant"].includes(item.role) && typeof item.content === "string")' in app_source


def test_frontend_scroll_contract_constrains_shell_and_preserves_compact_controls():
    repo = Path(__file__).resolve().parents[1]
    style_source = (repo / "static" / "style.css").read_text()

    def rule(source, selector):
        return source.split(f"{selector} {{", 1)[1].split("}", 1)[0]

    desktop_source = style_source.split("@media (max-width: 1024px)", 1)[0]
    portrait_mobile_source = style_source.split("@media (max-width: 1024px)", 1)[1].split(
        "@media (min-width: 1025px)", 1
    )[0]
    compact_source = style_source.split("@media (max-height: 500px) {", 1)[1].split(
        "@media (prefers-reduced-motion:", 1
    )[0]

    assert "html {\n  height: 100%;\n}" in desktop_source
    assert "height: 100dvh;" in rule(desktop_source, "body")
    assert "min-height: 100dvh;" in rule(desktop_source, "body")
    assert "overflow: hidden;" in rule(desktop_source, "body")
    assert "min-height: 0;\n  overflow: hidden;" in rule(desktop_source, ".layout")
    assert "min-height: 0;\n  overflow-y: auto;" in rule(desktop_source, ".panel")
    assert "overflow-y: auto;" in rule(desktop_source, ".response")
    assert "overflow-y: auto;" in rule(desktop_source, ".instruction-layer textarea")

    # The new bottom rail is a grid row; the active panel consumes the remainder.
    assert "54px minmax(0, 1fr) 70px" in portrait_mobile_source
    assert ".response { min-height: 0; }" in portrait_mobile_source
    assert "grid-row: 3; grid-column: 1" in portrait_mobile_source
    assert "height: var(--visual-height)" in portrait_mobile_source
    # Short windows keep the locked shell; context moves into a disclosure and
    # the tool list uses an opaque overlay instead of growing the document.
    assert "overflow: auto;" not in compact_source
    assert "overflow: visible;" not in compact_source
    assert "display: list-item;" in rule(compact_source, "#contextDisclosure > summary")
    assert "width: fit-content;" in rule(compact_source, "#contextDisclosure > summary")
    assert "position: fixed;" in rule(compact_source, ".tool-picker:not(.hidden)")
    assert "min-height: 0;" in rule(compact_source, ".tool-picker-list")
    assert "background: var(--panel);" in rule(desktop_source, ".tool-picker")


def test_chat_ui_rename_streaming_scroll_and_sidebar_contracts():
    """Rename saves every time, one reply streams at a time and redraws only its bubble,
    upward scrolling stops following, and the sidebar never reflows on hover."""
    repo = Path(__file__).resolve().parents[1]
    app_source = (repo / "static" / "app.js").read_text()
    style_source = (repo / "static" / "style.css").read_text()
    index_source = (repo / "static" / "index.html").read_text()
    main_source = (repo / "desktop" / "main.js").read_text()

    rename = app_source.split("async function renameConversation(cid, element) {", 1)[1].split("// NODE HANDLING", 1)[0]
    # Every listed conversation exists on the router, so a rename is always saved there.
    assert "convo.messages.length > 0" not in rename
    assert "const syncSuccess = await syncRenameToBackend(cid, newTitle);" in rename
    assert "convo.title = oldTitle;" in rename
    assert rename.count("if (settled) return;") == 2

    send = app_source.split("async function sendMessage() {", 1)[1].split("function renderEmptyChatState()", 1)[0]
    assert "if (state.streaming) { notifyConsole(" in send
    assert "conversation_id: cid," in send
    assert "await loadConversationHistory(cid);" in send
    assert "renderLiveMessage(streamingMsg);" in send
    tokens = send.split("if (data.token && !data.done) {", 1)[1].split("}", 1)[0]
    assert "renderMessages()" not in tokens
    assert "if (!readerCanSee(cid)) markConversationUnread(cid);" in send
    assert "setStreamingUi(false);" in send.split("} finally {", 1)[1]
    assert 'sendBtn.textContent = streaming ? "Stop" : "Send";' in app_source

    scroll_button = app_source.split("function updateScrollBottomButton() {", 1)[1].split("}", 1)[0]
    assert "state.autoScroll" not in scroll_button
    assert 'responseBox.addEventListener("wheel", (event) => {' in app_source
    assert "responseBox.scrollTop = state.autoScroll ? responseBox.scrollHeight : keptScrollTop;" in app_source
    assert "@keyframes stream-cursor-blink" in style_source
    assert 'indicator.style.animation = "blink 1s infinite"' not in app_source

    # Hover shows the row actions without changing the row's height.
    actions = style_source.split(".convo-actions {", 1)[1].split("}", 1)[0]
    assert "position: absolute;" in actions and "display: none" not in actions
    assert 'id="convoSort"' in index_source and 'data-convo-density="compact"' in index_source
    assert 'const UNREAD_CONVOS_KEY = "dave_unread_conversations";' in app_source
    assert "if (sequence !== switchSequence) return;" in app_source

    # Reply links leave the app window; it never opens other origins itself.
    assert "win.webContents.setWindowOpenHandler(" in main_source
    assert 'return { action: "deny" };' in main_source
    assert "shell.openExternal(url)" in main_source
    assert 'win.webContents.on("will-navigate"' in main_source
