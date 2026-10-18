import json
import subprocess
import sys
from pathlib import Path

from conftest import TEST_API_KEY


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record_monitor_status.py"
AUTH = {"X-API-Key": TEST_API_KEY}


def run_recorder(data_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--data-dir", str(data_dir), *extra],
        capture_output=True, text=True,
    )


def healthy_args(latency: str = "12.5") -> tuple[str, ...]:
    return (
        "--overall", "healthy", "--summary", "All targets healthy.",
        "--target", "Local", "http://127.0.0.1:8000/health", "healthy", latency, "200",
        "--target", "Walter", "http://100.64.0.2:11434/api/tags", "healthy", "85.7", "200",
    )


def test_recorder_writes_structured_status_and_bounded_history(tmp_path):
    for index in range(170):
        result = run_recorder(tmp_path, *healthy_args(str(index)))
        assert result.returncode == 0, result.stderr
    payload = json.loads((tmp_path / "on_call_status.json").read_text())
    assert payload["version"] == 1
    assert payload["current"]["targets"][0]["latency_ms"] == 169.0
    assert payload["current"]["recovery"] is None
    assert len(payload["history"]) == 168
    assert payload["history"][0]["targets"][0]["latency_ms"] == 2.0


def test_recorder_preserves_last_good_file_when_input_is_invalid(tmp_path):
    accepted = run_recorder(tmp_path, *healthy_args())
    assert accepted.returncode == 0
    path = tmp_path / "on_call_status.json"
    before = path.read_bytes()
    refused = run_recorder(
        tmp_path,
        "--overall", "healthy", "--summary", "bad",
        "--target", "Walter", "file:///tmp/tags", "healthy", "12", "200",
    )
    assert refused.returncode != 0
    assert path.read_bytes() == before


def test_recorder_accepts_outage_and_recovery_details(tmp_path):
    result = run_recorder(
        tmp_path,
        "--overall", "recovered", "--summary", "Walter recovered.",
        "--target", "Walter", "http://100.64.0.2:11434/api/tags", "recovered", "91.2", "200",
        "--recovery-node", "Walter", "--recovery-steps", "Started existing Ollama service.",
        "--recovery-seconds", "18.4",
    )
    assert result.returncode == 0, result.stderr
    recovery = json.loads((tmp_path / "on_call_status.json").read_text())["current"]["recovery"]
    assert recovery == {
        "node": "Walter",
        "steps": "Started existing Ollama service.",
        "elapsed_seconds": 18.4,
    }


def test_authenticated_monitor_endpoint_returns_empty_and_recorded_states(router_factory):
    router, client, _ = router_factory()
    assert client.get("/monitoring/on-call").status_code == 401
    empty = client.get("/monitoring/on-call", headers=AUTH)
    assert empty.status_code == 200
    assert empty.json()["current"] is None
    assert empty.json()["error"] == "No hourly status has been recorded yet."

    recorded = run_recorder(router.BASE_DIR, *healthy_args())
    assert recorded.returncode == 0, recorded.stderr
    response = client.get("/monitoring/on-call", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["current"]["overall"] == "healthy"
    assert response.json()["current"]["targets"][1]["name"] == "Walter"
    assert response.json()["error"] is None
