from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stage_wallboard as bundle
from serve_wallboard import make_server
from export_wallboard_status import project
from test_wallboard_projection import fixture


@pytest.fixture
def running(tmp_path):
    output = tmp_path / "bundle"
    bundle.stage(output)
    status = tmp_path / "status.json"
    status.write_text(json.dumps(project(fixture())))
    server = make_server(output, status, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, output, status
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(server, path, method="GET"):
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_loopback_assets_status_headers_and_head(running):
    server, output, status = running
    assert server.server_address[0] == "127.0.0.1"
    for path in ["/", *["/" + name for name in bundle.ASSETS], "/wallboard-status.json?t=1"]:
        code, headers, body = request(server, path)
        assert code == 200 and body
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
    code, headers, body = request(server, "/", "HEAD")
    assert code == 200 and body == b"" and int(headers["Content-Length"]) > 0


@pytest.mark.parametrize("path", ["/manifest.json", "/serve_wallboard.py", "/../source.json", "/%2e%2e/source.json", "/wallboard.html/", "//wallboard.html", "/.env"])
def test_non_allowlisted_requests_are_refused(running, path):
    assert request(running[0], path)[0] == 404


def test_write_methods_cannot_change_status(running):
    server, _, status = running
    original = status.read_bytes()
    for method in ["POST", "PUT", "DELETE", "PATCH"]:
        assert request(server, "/wallboard-status.json", method)[0] == 501
    assert status.read_bytes() == original


def test_untrusted_host_cannot_read_loopback_status(running):
    server = running[0]
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request("GET", "/wallboard-status.json", headers={"Host": "untrusted.invalid"})
        response = connection.getresponse()
        assert response.status == 403
        assert response.read() == b"Host refused\n"
    finally:
        connection.close()


def test_missing_invalid_and_symlink_status_fail_closed(running):
    server, output, status = running
    status.write_text('{"secret":"not for display"}')
    code, _, body = request(server, "/wallboard-status.json")
    assert code == 503 and b"secret" not in body
    status.unlink()
    assert request(server, "/wallboard-status.json")[0] == 503
    status.symlink_to(output / "wallboard.html")
    assert request(server, "/wallboard-status.json")[0] == 503


def test_unknown_projection_fields_never_escape(running):
    server, _, status = running
    data = json.loads(status.read_text())
    data["api_key"] = "secret-value"
    data["current"]["summary"] = "secret-value"
    status.write_text(json.dumps(data))
    code, _, body = request(server, "/wallboard-status.json")
    assert code == 200 and b"secret-value" not in body
    assert json.loads(body)["published_at"] == data["published_at"]


def test_bundle_refuses_existing_directory_and_detects_tampering(tmp_path):
    output = tmp_path / "bundle"
    bundle.stage(output)
    bundle.verify(output)
    with pytest.raises(FileExistsError):
        bundle.stage(output)
    (output / "wallboard.js").write_text("changed")
    with pytest.raises(ValueError):
        bundle.verify(output)


def test_symlink_asset_is_not_served(running):
    server, output, status = running
    (output / "wallboard.js").unlink()
    (output / "wallboard.js").symlink_to(status)
    assert request(server, "/wallboard.js")[0] == 503
