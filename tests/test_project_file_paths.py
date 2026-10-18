"""Project folder assignment and approved relative writes must agree on one destination."""
from dataclasses import replace

import httpx
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL
from test_agent_lifecycle_api import inventory, new_run, settled

AUTH = {"X-API-Key": TEST_API_KEY}


def test_folder_assignment_validation_and_roundtrip(router_factory, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    _, client, _ = router_factory(tools=True, tool_roots=[str(allowed)])
    for path in ("relative", "bad\x00path", str(tmp_path), str(allowed / "missing")):
        response = client.post('/projects', headers=AUTH, json={"name": "P", "output_directory": path})
        assert response.status_code == 422
    project = client.post('/projects', headers=AUTH, json={"name": "P", "output_directory": str(allowed)}).json()
    endpoint = f"/projects/{project['project_id']}"
    assert client.get(endpoint, headers=AUTH).json()['output_directory'] == str(allowed)
    assert client.put(endpoint, headers=AUTH, json={"output_directory": str(tmp_path)}).status_code == 422
    assert client.get(endpoint, headers=AUTH).json()['output_directory'] == str(allowed)
    assert client.put(endpoint, headers=AUTH, json={"output_directory": ""}).json()['output_directory'] == ""


def test_relative_write_uses_root_without_changing_cwd(router_factory, tmp_path):
    router, _, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    result = router.tool_file_write({"path": "nested/output.txt", "content": "ok"})
    assert result.status == 'success'
    assert (tmp_path / 'nested/output.txt').read_text() == 'ok'
    router.TOOL_ROOTS = [tmp_path, tmp_path / 'second']
    assert router.tool_file_write({"path": "ambiguous.txt", "content": "no"}).status == 'error'
    assert not (tmp_path / 'ambiguous.txt').exists()


def test_project_scope_rejects_traversal_siblings_and_symlinks(router_factory, tmp_path):
    project = tmp_path / 'project'
    sibling = tmp_path / 'sibling'
    project.mkdir()
    sibling.mkdir()
    (project / 'escape').symlink_to(sibling, target_is_directory=True)
    router, _, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    binding = router.HostRunBinding('default', '', 'm', 100, 0.0, 'p', None, None, {},
                                    project_directory=str(project))
    token = router.HOST_RUN_CONTEXT.set(binding)
    try:
        for path in ('../sibling/no.txt', str(sibling / 'no.txt'), 'escape/no.txt'):
            assert router.tool_file_write({'path': path, 'content': 'no'}).status == 'error'
        assert not (sibling / 'no.txt').exists()
        assert router.tool_file_append({'path': 'ok.txt', 'content': 'yes'}).status == 'success'
        assert router.tool_file_read({'path': 'ok.txt'}).result == 'yes'
        router.HOST_RUN_CONTEXT.set(replace(binding, project_directory=None))
        assert 'Assign an output folder' in router.tool_file_write({'path': 'no.txt', 'content': 'no'}).error
        router.HOST_RUN_CONTEXT.set(binding)
        router.TOOL_ROOTS = []
        assert router.tool_file_write({'path': 'revoked.txt', 'content': 'no'}).status == 'error'
    finally:
        router.HOST_RUN_CONTEXT.reset(token)


def test_approved_write_keeps_starting_project_directory(router_factory, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    router, client, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    project = client.post('/projects', headers=AUTH, json={"name": "P", "output_directory": str(first)}).json()
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        mock.post(f'{TEST_NODE_URL}/api/chat').side_effect = [
            httpx.Response(200, json={'message': {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'save', 'type': 'function', 'function': {'name': 'file.write', 'arguments': {
                    'path': 'node_status_monitor.py', 'content': 'print("saved")\n'}}}]}, 'done': True}),
            httpx.Response(200, json={'message': {'role': 'assistant', 'content': 'Saved.'}, 'done': True}),
        ]
        created = new_run(client, project_id=project['project_id'], selected_tools=['file.write'])
        assert created.status_code == 200, created.text
        run_id = created.json()['run_id']
        paused = settled(client, run_id)
        assert paused['status'] == 'approval_required'
        assert paused['context']['output_directory'] == str(first)
        assert not (first / 'node_status_monitor.py').exists()
        assert client.put(f"/projects/{project['project_id']}", headers=AUTH,
                          json={'output_directory': str(second)}).status_code == 200
        pending = paused['snapshot']['pending_call']
        decision = {key: pending[key] for key in ('call_id', 'digest', 'definition_fingerprint', 'permission', 'nonce')}
        approved = client.post(f'/tools/agent/runs/{run_id}/decisions', headers=AUTH,
                               json={**decision, 'decision': 'approve'})
        assert approved.status_code == 200, approved.text
        assert approved.json()['status'] == 'completed'
        assert (first / 'node_status_monitor.py').read_text() == 'print("saved")\n'
        assert not (second / 'node_status_monitor.py').exists()
