"""Offline contract tests for the standalone generated monitor."""
import json

import pytest

from node_status_monitor import StatusDisplay

HEALTHY = (
    'All targets healthy: local 127.0.0.1:8000 10.5 ms; '
    'sample 192.0.2.10:11434 20.125 ms. No recovery action needed.'
)


def test_healthy_summary():
    nodes, recovery = StatusDisplay().parse_status_line(HEALTHY)
    assert [(n.name, n.ip, n.port, n.latency, n.healthy) for n in nodes] == [
        ('local', '127.0.0.1', 8000, 10.5, True),
        ('sample', '192.0.2.10', 11434, 20.125, True),
    ]
    assert recovery is False


@pytest.mark.parametrize('ending', ['. No recovery action needed.', '; No recovery action needed', ' No recovery action needed.'])
@pytest.mark.parametrize('latency', ['0 ms', '2.125ms', '12 ms'])
def test_spacing_and_trailer_variants(ending, latency):
    nodes, recovery = StatusDisplay().parse_status_line(
        f'  All targets healthy: sample [2001:db8::1]:11434 {latency}{ending}  '
    )
    assert len(nodes) == 1
    assert nodes[0].ip == '2001:db8::1'
    assert nodes[0].latency == float(latency.replace('ms', '').strip())
    assert recovery is False


@pytest.mark.parametrize('line', [
    '', 'Some targets unhealthy: sample 192.0.2.10:11434 timeout',
    'All targets healthy: No recovery action needed.',
    HEALTHY.replace('20.125 ms', 'n/a ms'),
    HEALTHY.replace('20.125 ms', '-1 ms'),
    HEALTHY.replace(':11434', ':0'),
    HEALTHY.replace(':11434', ':65536'),
    HEALTHY.replace(':11434', ':bad'),
    HEALTHY.replace('No recovery action needed.', 'Recovery required.'),
    HEALTHY + ' trailing garbage',
    HEALTHY.replace('; sample', '; malformed; sample'),
])
def test_bad_or_unhealthy_input_is_explicit_and_preserves_previous_state(line):
    display = StatusDisplay()
    display.update_status(HEALTHY)
    previous = display.nodes
    with pytest.raises(ValueError):
        display.update_status(line)
    assert display.nodes is previous


def test_display_and_file_round_trip(tmp_path, capsys):
    path = tmp_path / 'node_status.json'
    display = StatusDisplay(str(path))
    display.update_status(HEALTHY)
    display.display()
    output = capsys.readouterr().out
    assert 'local' in output and 'sample' in output and '20.125 ms' in output
    display.save_to_file()
    data = json.loads(path.read_text())
    assert len(data['nodes']) == 2
    reopened = StatusDisplay(str(path))
    reopened.load_from_file()
    assert [vars(n) for n in reopened.nodes] == [vars(n) for n in display.nodes]
