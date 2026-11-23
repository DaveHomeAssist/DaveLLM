import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("wallboard_export", SCRIPTS / "export_wallboard_status.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def fixture():
    return {"version": 1, "current": {"checked_at": "2026-10-30T12:00:00Z", "overall": "healthy", "summary": "secret raw error /private/user", "targets": [{"name": name, "endpoint": "http://127.0.0.1:8000/health", "status": "healthy", "latency_ms": 12.5, "http_status": 200} for name in ["Local", "Dominic", "Walter", "Duncan"]], "recovery": None}, "history": [], "api_key": "private-value"}


def test_projection_drops_free_text_and_unknown_fields():
    payload = fixture()
    payload["history"] = [payload["current"]]
    result = exporter.project(payload)
    assert result["current"]["targets"] == payload["current"]["targets"]
    assert result["history"][0]["overall"] == "healthy"
    encoded = json.dumps(result)
    assert "private" not in encoded and "secret" not in encoded


@pytest.mark.parametrize("change", [
    lambda p: p.update(version=2),
    lambda p: p["current"].update(checked_at="yesterday"),
    lambda p: p["current"].update(checked_at="2026-10-30T12:00:00"),
    lambda p: p["current"]["targets"][0].update(endpoint="http://user:password@localhost/health"),
    lambda p: p["current"]["targets"][0].update(endpoint="http://localhost/health?key=secret"),
    lambda p: p["current"]["targets"][0].update(latency_ms=float("nan")),
    lambda p: p["current"]["targets"][0].update(http_status=True),
    lambda p: p["current"]["targets"][0].update(status="down"),
    lambda p: p["current"].update(targets=[]),
    lambda p: p.update(history=[p["current"]] * 169),
])
def test_projection_refuses_invalid_or_secret_bearing_values(change):
    payload = fixture()
    change(payload)
    with pytest.raises((ValueError, TypeError)):
        exporter.project(payload)


def test_invalid_export_preserves_existing_destination(tmp_path):
    import subprocess
    source, destination = tmp_path / "source.json", tmp_path / "display.json"
    source.write_text(json.dumps({"version": 3}))
    destination.write_text("retained")
    result = subprocess.run([sys.executable, str(SCRIPTS / "export_wallboard_status.py"), str(source), str(destination)], capture_output=True)
    assert result.returncode != 0
    assert destination.read_text() == "retained"
