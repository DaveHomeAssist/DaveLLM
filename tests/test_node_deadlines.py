"""Real HTTP reads: inactivity timeouts must not replace whole-reply deadlines."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from conftest import TEST_API_KEY
from davellm_ollama import ollama_chat


TOTAL = 0.25
UPPER_BOUND = 0.7  # Scheduling slack; still well below the 1.5 s stall / 2 s read timeout.
AUTH = {"X-API-Key": TEST_API_KEY}
REPLY = b'{"message":{"role":"assistant","content":"ok"},"done":true}'


@pytest.fixture
def slow_node():
    @contextmanager
    def serve(mode, status=200):
        stop = threading.Event()
        sent = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                payload = {"models": [{"name": "m", "model": "m"}]}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    if mode == "headers" and stop.wait(1.5):
                        return
                    if mode == "error_headers" and stop.wait(0.35):
                        return
                    self.send_response(status)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    pieces = [REPLY[i:i + 1] for i in range(len(REPLY))] if mode == "drip" else [REPLY]
                    for piece in pieces:
                        if mode == "drip" and stop.wait(0.04):
                            return
                        if mode in {"body", "error_headers"} and stop.wait(1.5):
                            return
                        self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                        self.wfile.flush()
                        sent.append(piece)
                    if mode == "eof" and stop.wait(1.5):
                        return
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The deadline closes the client while this node is still replying.
                finally:
                    self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", sent
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive()

    return serve


@pytest.mark.parametrize("mode", ["drip", "body", "eof", "headers"])
@pytest.mark.parametrize("status", [200, 503])
def test_complete_deadline_interrupts_real_http_reads(slow_node, mode, status):
    with slow_node(mode, status=status) as (url, sent):
        started = time.monotonic()
        with pytest.raises(httpx.ReadTimeout):
            ollama_chat(url, "m", [{"role": "user", "content": "hi"}],
                        stream=False, timeout=2, total_timeout=TOTAL)
        assert TOTAL <= time.monotonic() - started < UPPER_BOUND
        if mode == "drip":
            assert 1 < len(sent) < len(REPLY)  # Progress cannot reset the total.
        if mode == "eof":
            assert sent == [REPLY]  # Even a complete JSON value must finish its HTTP body in time.


@pytest.mark.parametrize("endpoint", ["/chat", "/tools/agent/run", "/tools/agent/runs"])
def test_slow_drip_preserves_route_timeout_contracts(slow_node, router_factory, monkeypatch, endpoint):
    # Agent payloads include the full tool catalog. Allow their serialization and
    # client setup under coverage, but expire before the >2 s dripped reply ends.
    budget, upper = (TOTAL, UPPER_BOUND) if endpoint == "/chat" else (1.0, 1.7)
    monkeypatch.setenv("DAVE_NODE_TOTAL_TIMEOUT", str(budget))
    with slow_node("drip") as (url, sent):
        router, client, _ = router_factory(tools=True, nodes=[{"id": "node-test", "name": "Test Ollama", "url": url}])
        # Separate the operation timeout from the total; only the wall clock may end this reply.
        monkeypatch.setattr(router, "node_complete_timeout", lambda: httpx.Timeout(2))
        model_times = []
        invoke = router.ollama_chat_bounded

        async def timed_model(*args, **kwargs):
            model_times.append(time.monotonic())
            try:
                return await invoke(*args, **kwargs)
            finally:
                model_times.append(time.monotonic())

        monkeypatch.setattr(router, "ollama_chat_bounded", timed_model)
        with client:
            assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
            body = {"node_id": "node-test", "model": "m"}
            body.update({"conversation_id": "deadline", "prompt": "hi"} if endpoint == "/chat"
                        else {"messages": [{"role": "user", "content": "hi"}]})
            started = time.monotonic()
            response = client.post(endpoint, headers=AUTH, json=body)
            if endpoint == "/chat":
                assert response.status_code == 504
                assert response.json() == {"detail": "Node 'Test Ollama' timed out"}
                assert not [m for m in router.CONVERSATIONS["deadline"]["messages"] if m["role"] == "assistant"]
                assert TOTAL <= time.monotonic() - started < UPPER_BOUND
            else:
                assert response.status_code == 200
                result = response.json()
                while result["status"] in {"created", "running"} and time.monotonic() - started < 3:
                    time.sleep(0.01)
                    result = client.get(f"/tools/agent/runs/{result['run_id']}", headers=AUTH).json()
                assert result["status"] == "model_timeout"
                # Run creation checks tool provenance before the model step. Measure
                # the real I/O separately from that CPU work and lifecycle polling.
                assert len(model_times) == 2
                assert budget * 0.8 <= model_times[1] - model_times[0] < upper
                assert time.monotonic() - started < 3
            assert 1 < len(sent) < len(REPLY)
            assert router.NODE_ACTIVITY.in_flight(url) == 0


@pytest.mark.parametrize("mode, budget, upper", [("body", TOTAL, UPPER_BOUND), ("error_headers", 0.5, 0.75)])
def test_stalled_stream_error_body_keeps_sse_timeout_contract(slow_node, router_factory, monkeypatch, mode, budget, upper):
    # Delayed headers consume 350 ms of the 500 ms budget; resetting it would take >=850 ms.
    monkeypatch.setenv("DAVE_NODE_FIRST_CHUNK_TIMEOUT", str(budget))
    with slow_node(mode, status=503) as (url, _sent):
        router, client, _ = router_factory(nodes=[{"id": "node-test", "name": "Test Ollama", "url": url}])
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        started = time.monotonic()
        response = client.post("/chat/stream", headers=AUTH, json={
            "conversation_id": "deadline", "prompt": "hi", "node_id": "node-test", "model": "m",
        })
        assert budget <= time.monotonic() - started < upper
        assert response.status_code == 200
        events = [json.loads(block[6:]) for block in response.text.split("\n\n") if block.startswith("data: ")]
        assert [event for event in events if "error" in event] == [{"error": "Node timed out", "done": True}]
        assert events[-1] == {"token": "", "done": True, "message_count": 1}
        assert not [m for m in router.CONVERSATIONS["deadline"]["messages"] if m["role"] == "assistant"]
        assert router.NODE_ACTIVITY.in_flight(url) == 0
