"""Unit tests for davellm_ollama, the native /api/chat transport (no router import)."""

import ast
import json
from pathlib import Path

import httpx
import pytest
import respx

from davellm_ollama import (
    OllamaResponseError,
    OllamaStreamError,
    build_ollama_chat_payload,
    ollama_chat,
    ollama_image_data,
    ollama_user_message,
    parse_ollama_chat_line,
    parse_ollama_chat_response,
)

ROOT = Path(__file__).resolve().parents[1]
NODE_URL = "http://ollama.test:11434"
MESSAGES = [{"role": "user", "content": "hi"}]


def ndjson(*lines):
    return "\n".join(json.dumps(line) if isinstance(line, dict) else line for line in lines) + "\n"


def test_payload_defaults_send_only_native_keys():
    payload = build_ollama_chat_payload("m", MESSAGES, stream=False, num_predict=2048, temperature=0.7)
    assert payload == {
        "model": "m",
        "messages": MESSAGES,
        "stream": False,
        "options": {"top_p": 1.0, "num_predict": 2048, "temperature": 0.7},
    }
    for absent in ("keep_alive", "think", "max_tokens", "images", "temperature"):
        assert absent not in payload

    without_predict = build_ollama_chat_payload("m", MESSAGES, stream=True, num_predict=None, temperature=0.7)
    assert without_predict["stream"] is True
    assert without_predict["options"] == {"top_p": 1.0, "temperature": 0.7}

    bare = build_ollama_chat_payload("m", MESSAGES, stream=False)
    assert bare == {"model": "m", "messages": MESSAGES, "stream": False, "options": {"top_p": 1.0}}


def test_payload_caller_options_merge_last_and_keep_alive_passthrough():
    payload = build_ollama_chat_payload(
        "m", MESSAGES, stream=False, num_predict=64, temperature=0.7,
        options={"num_ctx": 8192, "temperature": 0.1, "seed": None},
        keep_alive="10m", think=False,
    )
    assert payload["options"] == {"top_p": 1.0, "num_predict": 64, "temperature": 0.1, "num_ctx": 8192}
    assert payload["keep_alive"] == "10m"
    assert payload["think"] is False

    zero = build_ollama_chat_payload("m", MESSAGES, stream=False, keep_alive=0)
    assert zero["keep_alive"] == 0
    assert zero["options"] == {"top_p": 1.0}

    # /v1 parity baseline is overridable by the caller.
    override = build_ollama_chat_payload("m", MESSAGES, stream=False, options={"top_p": 0.9})
    assert override["options"] == {"top_p": 0.9}


def test_image_data_strips_data_url_prefix_only():
    images = [
        "data:image/png;base64,QUJD", "data:;base64,QUJD", "QUJD", " QUJD\n", "DATA:image/jpeg;base64,QUJD",
        "data:image/png;charset=utf-8;base64,QUJD", "data:image/png;name=a.png;base64,QUJD",
    ]
    assert ollama_image_data(images) == ["QUJD"] * 7
    # A non-base64 data URL is not image data we can strip; it passes through unchanged.
    assert ollama_image_data(["data:text/plain,QUJD"]) == ["data:text/plain,QUJD"]
    assert ollama_user_message("", ["QUJD"]) == {"role": "user", "content": "", "images": ["QUJD"]}
    assert ollama_user_message("look", []) == {"role": "user", "content": "look"}


def test_parse_line_tolerance_thinking_done_and_error():
    for ignored in ("", "   ", "[DONE]", 'data: {"message": {"content": "x"}}', '"str"', "[1, 2]"):
        assert parse_ollama_chat_line(ignored) is None

    thinking = parse_ollama_chat_line(json.dumps({"message": {"role": "assistant", "content": "", "thinking": "x"}, "done": False}))
    assert (thinking.content, thinking.thinking, thinking.done, thinking.metrics) == ("", "x", False, None)

    done = parse_ollama_chat_line(json.dumps({
        "message": {"role": "assistant", "content": "!"},
        "done": True, "done_reason": "stop", "eval_count": 2, "total_duration": 10, "load_duration": 0,
    }))
    assert done.content == "!"
    assert done.done is True
    assert done.done_reason == "stop"
    assert done.metrics.eval_count == 2
    assert done.metrics.total_duration == 10
    assert done.metrics.prompt_eval_cached_count is None

    with pytest.raises(OllamaStreamError, match="boom"):
        parse_ollama_chat_line(json.dumps({"error": "boom"}))


def test_parse_response_requires_native_shape():
    result = parse_ollama_chat_response({
        "model": "m", "created_at": "t",
        "message": {"role": "assistant", "content": "Hi", "thinking": "hmm"},
        "done": True, "done_reason": "stop", "eval_count": 3,
    })
    assert (result.content, result.thinking, result.done_reason, result.model) == ("Hi", "hmm", "stop", "m")
    assert result.metrics.eval_count == 3

    for bad in (
        {"choices": [{"message": {"content": "x"}}]},
        {"message": {"content": None}},
        {"message": {"role": "assistant"}},
        {"error": "x"},
        "text",
        None,
    ):
        with pytest.raises(OllamaResponseError):
            parse_ollama_chat_response(bad)


def test_complete_posts_native_endpoint_and_maps_http_errors():
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{NODE_URL}/api/chat").mock(
            return_value=httpx.Response(200, json={"message": {"role": "assistant", "content": "ok"}, "done": True})
        )
        result = ollama_chat(NODE_URL + "/", "m", MESSAGES, stream=False, timeout=5, num_predict=8)
        assert result.content == "ok"
        request = route.calls.last.request
        assert str(request.url) == f"{NODE_URL}/api/chat"
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert body["stream"] is False
        assert body["options"] == {"top_p": 1.0, "num_predict": 8}

        route.mock(return_value=httpx.Response(503, text="node offline"))
        with pytest.raises(httpx.HTTPStatusError) as status_error:
            ollama_chat(NODE_URL, "m", MESSAGES, stream=False, timeout=5)
        assert status_error.value.response.text == "node offline"

        route.mock(side_effect=httpx.ReadTimeout("t"))
        with pytest.raises(httpx.TimeoutException):
            ollama_chat(NODE_URL, "m", MESSAGES, stream=False, timeout=5)

        route.mock(side_effect=httpx.ConnectError("c"))
        with pytest.raises(httpx.ConnectError):
            ollama_chat(NODE_URL, "m", MESSAGES, stream=False, timeout=5)

        route.mock(return_value=httpx.Response(200, text="not json"))
        with pytest.raises(OllamaResponseError):
            ollama_chat(NODE_URL, "m", MESSAGES, stream=False, timeout=5)


@pytest.mark.asyncio
async def test_timeout_kwarg_reaches_httpx_on_both_paths():
    """The call sites' 120 s / 10 s timeouts are a contract; the helper must not drop the kwarg."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{NODE_URL}/api/chat").mock(
            return_value=httpx.Response(200, json={"message": {"role": "assistant", "content": "ok"}, "done": True})
        )
        ollama_chat(NODE_URL, "m", MESSAGES, stream=False, timeout=7)
        assert route.calls.last.request.extensions["timeout"] == httpx.Timeout(7).as_dict()

        route.mock(
            return_value=httpx.Response(
                200,
                content=ndjson({"message": {"role": "assistant", "content": "ok"}, "done": True}),
                headers={"content-type": "application/x-ndjson"},
            )
        )
        async with ollama_chat(NODE_URL, "m", MESSAGES, stream=True, timeout=9) as stream:
            async for _ in stream:
                pass
        assert route.calls.last.request.extensions["timeout"] == httpx.Timeout(9).as_dict()


@pytest.mark.asyncio
async def test_stream_accumulates_thinking_content_metrics_and_stops_at_done():
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{NODE_URL}/api/chat").mock(
            return_value=httpx.Response(
                200,
                content=ndjson(
                    {"message": {"role": "assistant", "content": "", "thinking": "hmm"}, "done": False},
                    {"message": {"role": "assistant", "content": "Hi"}, "done": False},
                    {"message": {"role": "assistant", "content": "!"}, "done": True, "done_reason": "stop", "eval_count": 2},
                    {"message": {"role": "assistant", "content": "never"}, "done": False},
                ),
                headers={"content-type": "application/x-ndjson"},
            )
        )
        stream = ollama_chat(NODE_URL, "m", MESSAGES, stream=True, timeout=5, num_predict=4, keep_alive="1m")
        assert stream.endpoint == f"{NODE_URL}/api/chat"
        assert stream.payload["stream"] is True
        assert stream.payload["keep_alive"] == "1m"
        assert route.calls.call_count == 0  # no I/O before __aenter__

        contents = []
        async with stream as opened:
            assert opened is stream
            async for chunk in stream:
                contents.append(chunk.content)

        assert contents == ["", "Hi", "!"]
        assert stream.content == "Hi!"
        assert stream.thinking == "hmm"
        assert stream.completed is True
        assert stream.done_reason == "stop"
        assert stream.metrics.eval_count == 2
        assert json.loads(route.calls.last.request.content)["options"] == {"top_p": 1.0, "num_predict": 4}


@pytest.mark.asyncio
async def test_stream_error_line_and_status_error():
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{NODE_URL}/api/chat").mock(
            return_value=httpx.Response(
                200,
                content=ndjson(
                    {"message": {"role": "assistant", "content": "partial"}, "done": False},
                    {"error": "runner died"},
                ),
                headers={"content-type": "application/x-ndjson"},
            )
        )
        stream = ollama_chat(NODE_URL, "m", MESSAGES, stream=True, timeout=5)
        with pytest.raises(OllamaStreamError, match="runner died"):
            async with stream:
                async for _ in stream:
                    pass
        assert stream.content == "partial"
        assert stream.completed is False

        route.mock(return_value=httpx.Response(503, text="node offline"))
        stream = ollama_chat(NODE_URL, "m", MESSAGES, stream=True, timeout=5)
        with pytest.raises(httpx.HTTPStatusError) as status_error:
            async with stream:
                raise AssertionError("__aenter__ must raise on non-2xx")
        assert status_error.value.response.status_code == 503
        assert status_error.value.response.content == b"node offline"
        assert status_error.value.response.is_closed
        assert stream._stack is None  # client and response were closed by __aenter__

        route.mock(
            return_value=httpx.Response(
                200,
                content=ndjson({"message": {"role": "assistant", "content": "cut"}, "done": False}),
                headers={"content-type": "application/x-ndjson"},
            )
        )
        stream = ollama_chat(NODE_URL, "m", MESSAGES, stream=True, timeout=5)
        async with stream:
            async for _ in stream:
                pass
        assert stream.content == "cut"
        assert stream.completed is False
        assert stream.metrics is None


def test_harness_paths_still_post_to_openai_compatible_endpoint():
    """The tool loop keeps /v1 (it exchanges tool schemas and tool_calls) until DL-TRANSPORT-01b.

    Only string constants in code count: a comment or docstring may mention the endpoint, but
    the two harness functions must post to it and no other code in app.py may.
    """
    source = (ROOT / "app.py").read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    harness = {
        node.name: (node.lineno, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ("invoke_harness_model", "run_agent_endpoint")
    }
    assert set(harness) == {"invoke_harness_model", "run_agent_endpoint"}
    for name, (start, end) in harness.items():
        body = "\n".join(lines[start - 1:end])
        assert "/v1/chat/completions" in body, name
        assert "ollama_chat(" not in body, name

    docstring_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstring_lines.update(range(first.lineno, first.end_lineno + 1))
    code_hits = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "/v1/chat/completions" in node.value and node.lineno not in docstring_lines
    )
    assert code_hits, "the harness call sites must be real string constants"
    for lineno in code_hits:
        assert any(start <= lineno <= end for start, end in harness.values()), f"app.py:{lineno} posts to /v1 outside the harness"
