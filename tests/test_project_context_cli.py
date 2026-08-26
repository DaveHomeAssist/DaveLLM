from scripts import project_context_cli


def test_pin_command_reads_current_revision_and_appends_pinned_text(monkeypatch):
    calls = []

    def fake_request(api_base, api_key, method, path, payload=None):
        calls.append((api_base, api_key, method, path, payload))
        if method == "GET":
            return {"revision": 7, "pinned_text": "Existing decision"}
        return {"revision": 8, **(payload or {})}

    monkeypatch.setattr(project_context_cli, "request_json", fake_request)
    args = project_context_cli.build_parser().parse_args(
        ["--api-base", "http://router.test", "pin", "proj one", "--text", "New fact"]
    )
    result = project_context_cli.run(args, "secret")

    assert calls[0][2:] == ("GET", "/projects/proj%20one/brain", None)
    assert calls[1][2] == "PUT"
    assert calls[1][4] == {
        "pinned_text": "Existing decision\nNew fact",
        "expected_revision": 7,
    }
    assert result["revision"] == 8


def test_edit_command_accepts_utf8_tier_files(tmp_path, monkeypatch):
    active_file = tmp_path / "active.md"
    active_file.write_text("Goal: finish verification.", encoding="utf-8")
    captured = {}

    def fake_request(api_base, api_key, method, path, payload=None):
        captured.update(payload or {})
        return {"revision": 3}

    monkeypatch.setattr(project_context_cli, "request_json", fake_request)
    args = project_context_cli.build_parser().parse_args(
        [
            "edit",
            "proj_1",
            "--active-file",
            str(active_file),
            "--compact-threshold",
            "2048",
            "--expected-revision",
            "2",
        ]
    )
    project_context_cli.run(args, "secret")

    assert captured == {
        "active_text": "Goal: finish verification.",
        "compact_threshold": 2048,
        "expected_revision": 2,
    }
