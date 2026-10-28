import io
import json
import subprocess
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from scripts import davellm_ask as cli


class Reply(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())


def args(*extra):
    return cli.build_parser().parse_args(
        ["--node", "fixture-node", "--model", "fixture-model", *extra, "Reply OK"]
    )


def fixture_router(monkeypatch, completion=None, inventory=None):
    calls = []
    replies = [
        inventory if inventory is not None else {"models": [{"id": "fixture-model"}], "error": None},
        completion if completion is not None else {"response": "OK", "done_reason": "stop", "model": "fixture-model"},
    ]

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            result = replies[len(calls) - 1]
            if isinstance(result, Exception):
                raise result
            return Reply(result)

    monkeypatch.setattr(cli, "build_opener", lambda *handlers: Opener())
    return calls


def test_inventory_precedes_one_stateless_completion(monkeypatch):
    calls = fixture_router(monkeypatch)
    assert cli.ask(args("--max-tokens", "64"), "private-key")["response"] == "OK"
    assert len(calls) == 2
    inventory, completion = calls
    assert inventory[0].full_url == "http://127.0.0.1:8000/nodes/fixture-node/models"
    assert inventory[0].method == "GET"
    assert inventory[1] == 15
    request, timeout = completion
    assert request.full_url == "http://127.0.0.1:8000/eval/chat"
    assert request.method == "POST"
    assert request.get_header("X-api-key") == "private-key"
    assert timeout == 180
    assert json.loads(request.data) == {
        "node_id": "fixture-node", "model": "fixture-model",
        "messages": [{"role": "user", "content": "Reply OK"}],
        "max_tokens": 64, "temperature": 0,
    }


@pytest.mark.parametrize("inventory", [
    {"models": []}, {"models": [{"id": "another-model"}]},
    {"models": [], "error": "offline"}, {"models": None},
])
def test_missing_inventory_or_model_never_dispatches_inference(monkeypatch, inventory):
    calls = fixture_router(monkeypatch, inventory=inventory)
    with pytest.raises(cli.CommandError):
        cli.ask(args(), "private-key")
    assert len(calls) == 1


@pytest.mark.parametrize("completion", [
    {"response": "", "done_reason": "stop"},
    {"response": "  ", "done_reason": "stop"},
    {"response": "notice", "reason": "tool_call_only", "done_reason": "stop"},
    {"response": "partial", "done_reason": "length"},
    {"response": "missing stop reason"},
    {"response": None, "done_reason": "stop"},
])
def test_empty_tool_only_or_incomplete_replies_fail_without_retry(monkeypatch, completion):
    calls = fixture_router(monkeypatch, completion=completion)
    with pytest.raises(cli.CommandError):
        cli.ask(args(), "private-key")
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [
    HTTPError("http://127.0.0.1:8000/eval/chat", 401, "raw private-key", {}, io.BytesIO(b"private-key")),
    HTTPError("http://127.0.0.1:8000/eval/chat", 302, "raw private-key", {}, io.BytesIO(b"private-key")),
    TimeoutError("private-key"), URLError("private-key"),
    IncompleteRead(b"private-key"),
])
def test_request_failure_has_no_stdout_secret_or_retry(monkeypatch, capsys, failure):
    calls = fixture_router(monkeypatch, completion=failure)
    monkeypatch.setenv("DAVE_API_KEY", "private-key")
    assert cli.main(["--node", "fixture-node", "--model", "fixture-model", "Reply OK"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-key" not in captured.err
    assert len(calls) == 2


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_outputs_answer_or_json(monkeypatch, capsys, as_json):
    fixture_router(monkeypatch)
    monkeypatch.setenv("DAVE_API_KEY", "private-key")
    argv = ["--node", "fixture-node", "--model", "fixture-model", "Reply OK"]
    if as_json:
        argv.insert(0, "--json")
    assert cli.main(argv) == 0
    result = capsys.readouterr()
    assert result.err == ""
    assert (json.loads(result.out)["response"] if as_json else result.out.strip()) == "OK"


def test_environment_key_does_not_access_keychain(monkeypatch):
    monkeypatch.setenv("DAVE_API_KEY", "private-key")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("Keychain read"))
    assert cli.load_api_key() == "private-key"


def test_existing_mac_keychain_item_is_used_without_exposing_it(monkeypatch):
    monkeypatch.delenv("DAVE_API_KEY", raising=False)
    monkeypatch.delenv("DAVE_KEYCHAIN_ACCOUNT", raising=False)
    monkeypatch.delenv("DAVE_KEYCHAIN_SERVICE", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "fixture-user")
    captured = []

    def keychain(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "private-key\n", "")

    monkeypatch.setattr(cli.subprocess, "run", keychain)
    assert cli.load_api_key() == "private-key"
    assert captured[0][0] == ["/usr/bin/security", "find-generic-password", "-a", "fixture-user",
                               "-s", "com.davellm.api-key", "-w"]
    assert captured[0][1]["capture_output"] is True
    assert captured[0][1]["timeout"] == 10


def test_missing_key_does_not_send_request(monkeypatch, capsys):
    monkeypatch.delenv("DAVE_API_KEY", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "build_opener", lambda *a: pytest.fail("Network request"))
    assert cli.main(["--node", "fixture-node", "--model", "fixture-model", "Reply OK"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("arguments", [
    ["--api-base", "http://outside.example"],
    ["--api-base", "http://127.0.0.1:8000/path"],
    ["--api-base", "http://user:pass@localhost:8000"],
    ["--api-base", "http://[invalid"],
    ["--timeout", "nan"], ["--timeout", "inf"], ["--timeout", "0"],
    ["--max-tokens", "0"], ["--max-tokens", "32769"],
])
def test_bad_arguments_fail_before_credential_access(monkeypatch, arguments):
    monkeypatch.setattr(cli, "load_api_key", lambda: pytest.fail("Credential access"))
    with pytest.raises(SystemExit) as exc:
        cli.main([*arguments, "--node", "fixture-node", "--model", "fixture-model", "Reply OK"])
    assert exc.value.code == 2


def test_redirect_handler_will_not_forward_credentials():
    request = Request("http://127.0.0.1:8000/eval/chat", headers={"X-API-Key": "private-key"})
    assert cli.NoRedirect().redirect_request(request, None, 302, "Found", {}, "http://outside.example") is None


def test_invalid_reply_shape_is_not_a_success():
    class Opener:
        def open(self, *a, **kw):
            return Reply(["invalid"])

    with pytest.raises(cli.CommandError, match="shape"):
        cli.request_json(Opener(), "http://127.0.0.1:8000", "private-key", "/eval/chat", 1)
