"""Readiness is not inference, write acceptance, or running-router registration."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
import respx

from fake_notion import FakeNotion, new_id, rt
from scripts import check_notion_adapter as cli

TOKEN = "readiness-test-token-not-a-real-secret"


def configured(page_id=None):
    return {"DAVE_ENABLE_TOOLS": "true", "DAVE_ENABLE_NOTION_TOOLS": "true",
            "DAVE_NOTION_TOKEN": TOKEN,
            "DAVE_NOTION_PAGES": json.dumps({"adapter-test": page_id or new_id()})}


def check(environ, **kwargs):
    return asyncio.run(cli.check_adapter(environ, **kwargs))


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_configuration_only_contacts_nothing(value):
    env = configured()
    env["DAVE_ENABLE_TOOLS"] = env["DAVE_ENABLE_NOTION_TOOLS"] = value
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        report = check(env)
    assert len(mock.calls) == 0
    assert report["configuration_ready"] is True
    assert report["live"] == "not_checked"
    assert report["scope"] == "this_process_environment"
    assert report["router_registration"] == report["write_acceptance"] == "not_checked"


@pytest.mark.parametrize("name,value,issue", [
    ("DAVE_ENABLE_TOOLS", "false", "DAVE_ENABLE_TOOLS must be enabled"),
    ("DAVE_ENABLE_NOTION_TOOLS", "no", "DAVE_ENABLE_NOTION_TOOLS must be enabled"),
    ("DAVE_ENABLE_NOTION_TOOLS", "unexpected", "DAVE_ENABLE_NOTION_TOOLS must be enabled"),
    ("DAVE_NOTION_TOKEN", "", "DAVE_NOTION_TOKEN is unset"),
    ("DAVE_NOTION_TOKEN", "bad secret", "DAVE_NOTION_TOKEN is invalid"),
    ("DAVE_NOTION_PAGES", "", "DAVE_NOTION_PAGES is unset or empty"),
    ("DAVE_NOTION_PAGES", "{}", "DAVE_NOTION_PAGES is invalid"),
    ("DAVE_NOTION_PAGES", "[]", "DAVE_NOTION_PAGES is invalid"),
    ("DAVE_NOTION_PAGES", '{"adapter-test":"bad id"}', "DAVE_NOTION_PAGES is invalid"),
])
def test_bad_configuration_refuses_even_explicit_live(name, value, issue):
    env = configured()
    env[name] = value
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        report = check(env, live=True)
    assert len(mock.calls) == 0
    assert report["configuration_ready"] is False
    assert issue in report["issues"]
    assert report["live"] == "not_checked"


def test_unconfigured_alias_is_not_echoed_or_contacted():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        report = check(configured(), page=TOKEN, live=True)
    assert len(mock.calls) == 0
    assert report["issues"] == ["The selected page alias is not configured"]
    assert TOKEN not in json.dumps(report)


def test_live_read_uses_existing_adapter_gets_only_and_redacts_content():
    fake = FakeNotion()
    page_id = fake.add_page("private title")
    block_id = fake.add_block(page_id, "paragraph", [rt("private body")])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        fake.mount(mock)
        report = check(configured(page_id), live=True)
    assert report["live"] == "readable"
    assert report["blocks_read"] == 1
    assert report["truncated"] is False
    assert report["children_not_read"] == 0
    assert len(fake.requests) == 2
    assert all(request.method == "GET" for request in fake.requests)
    assert all(request.url.host == "api.notion.com" for request in fake.requests)
    assert all(request.headers["Authorization"] == f"Bearer {TOKEN}" for request in fake.requests)
    for private in (TOKEN, page_id, block_id, "private title", "private body", "api.notion.com"):
        assert private not in json.dumps(report)


@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (404, "object_not_found"),
                                         (403, "restricted_resource")])
def test_live_api_refusal_is_not_readiness(status, code):
    fake = FakeNotion()
    page_id = fake.add_page()
    fake.fail("GET", r"/pages/.*", "reject", status=status, code=code)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        fake.mount(mock)
        report = check(configured(page_id), live=True)
    assert report["configuration_ready"] is True
    assert report["live"] == "failed"
    assert len(report["issues"]) == 1
    assert all(request.method == "GET" for request in fake.requests)


@pytest.mark.parametrize("truncated,unread", [(True, False), (False, True), (True, True)])
def test_partial_read_is_explicit(monkeypatch, truncated, unread):
    async def partial(*args, **kwargs):
        return {"title": TOKEN, "blocks": [{"text": TOKEN, "children_not_read": unread}],
                "truncated": truncated}

    monkeypatch.setattr(cli, "read_page", partial)
    report = check(configured(), live=True)
    assert report["live"] == "readable_partial"
    assert report["truncated"] is truncated
    assert report["children_not_read"] == int(unread)
    assert TOKEN not in json.dumps(report)


def test_unexpected_error_cannot_leak_credentials(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError(f"Bearer {TOKEN}: private content")

    monkeypatch.setattr(cli, "read_page", broken)
    report = check(configured(), live=True)
    assert report["live"] == "failed"
    assert TOKEN not in json.dumps(report)
    assert "private content" not in json.dumps(report)


@pytest.mark.parametrize("ready,live_status,exit_code", [
    (True, "not_checked", 0), (False, "not_checked", 1),
    (True, "readable", 0), (True, "readable_partial", 0), (True, "failed", 1),
])
def test_cli_flags_json_and_exit_codes(monkeypatch, capsys, ready, live_status, exit_code):
    async def stub(environ, *, page, live):
        assert page == "chosen" and live is True
        return {"configuration_ready": ready, "live": live_status}

    monkeypatch.setattr(cli, "check_adapter", stub)
    assert cli.main(["--page", "chosen", "--live"]) == exit_code
    assert json.loads(capsys.readouterr().out)["live"] == live_status


def test_standalone_import_and_cli_create_no_persistence(tmp_path):
    env = {name: value for name, value in os.environ.items() if not name.startswith("DAVE_")}
    env["PYTHONPATH"] = str(cli.ROOT)
    env["DAVE_DATA_DIR"] = str(tmp_path)
    imported = subprocess.run(
        [sys.executable, "-c", "import sys; from scripts import check_notion_adapter; "
         "assert 'app' not in sys.modules"], cwd=tmp_path, env=env, capture_output=True, text=True,
        timeout=10,
    )
    assert imported.returncode == 0, imported.stderr
    result = subprocess.run(
        [sys.executable, str(cli.ROOT / "scripts/check_notion_adapter.py")], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["configuration_ready"] is False
    assert json.loads(result.stdout)["live"] == "not_checked"
    assert list(tmp_path.iterdir()) == []
