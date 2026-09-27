"""Unit tests for davellm_ollama, the native /api/chat transport (no router import)."""

import ast
import json
from pathlib import Path

import httpx
import pytest
import respx

from davellm_ollama import (
    OllamaChatError,
    OllamaResponseError,
    OllamaStreamError,
    build_ollama_chat_payload,
    model_is_loaded,
    native_tool_messages,
    ollama_chat,
    ollama_chat_bounded,
    ollama_image_data,
    ollama_loaded_models,
    ollama_tool_call_names,
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


TOOL_CALL = {"function": {"name": "browser.run", "arguments": {"query": "weather"}}}


def test_tool_calls_are_surfaced_on_lines_and_results_and_malformed_ones_are_ignored():
    line = parse_ollama_chat_line(json.dumps({
        "message": {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL, "junk", 3]}, "done": False,
    }))
    assert line.content == ""
    assert line.tool_calls == (TOOL_CALL,)

    plain = parse_ollama_chat_line(json.dumps({"message": {"role": "assistant", "content": "x"}, "done": False}))
    assert plain.tool_calls == ()
    for malformed in ({}, "browser.run", None, {"function": {"name": "x"}}):
        chunk = parse_ollama_chat_line(json.dumps({"message": {"content": "", "tool_calls": malformed}, "done": False}))
        assert chunk.tool_calls == ()

    result = parse_ollama_chat_response({
        "message": {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]}, "done": True, "done_reason": "stop",
    })
    assert (result.content, result.tool_calls) == ("", (TOOL_CALL,))
    assert parse_ollama_chat_response({"message": {"role": "assistant", "content": "Hi"}}).tool_calls == ()


def test_tool_call_names_keep_order_and_skip_calls_without_a_string_name():
    calls = [
        TOOL_CALL,
        {"function": {"name": "web.search"}},
        {"function": {"name": 7}},
        {"function": "browser.open"},
        {"name": "top-level-is-not-native"},
        {},
    ]
    assert ollama_tool_call_names(calls) == ["browser.run", "web.search"]
    assert ollama_tool_call_names([]) == []


@pytest.mark.asyncio
async def test_stream_accumulates_tool_calls_across_lines():
    second = {"function": {"name": "browser.open", "arguments": {}}}
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{NODE_URL}/api/chat").mock(
            return_value=httpx.Response(
                200,
                content=ndjson(
                    {"message": {"role": "assistant", "content": "", "thinking": "search"}, "done": False},
                    {"message": {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]}, "done": False},
                    {"message": {"role": "assistant", "content": "", "tool_calls": [second]}, "done": True, "done_reason": "stop"},
                ),
                headers={"content-type": "application/x-ndjson"},
            )
        )
        seen = []
        async with ollama_chat(NODE_URL, "m", MESSAGES, stream=True, timeout=5) as stream:
            async for chunk in stream:
                seen.append(chunk.tool_calls)
    assert seen == [(), (TOOL_CALL,), (second,)]
    assert stream.tool_calls == [TOOL_CALL, second]
    assert stream.content == ""
    assert stream.completed is True


def test_no_app_code_posts_to_the_openai_compatible_endpoint():
    """DL-TRANSPORT-01b: the tool loop uses native /api/chat too, so /v1 is gone from app.py code.

    Only string constants in code count (a comment or docstring may still name /v1), and both
    harness functions must go through ollama_chat_bounded.
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
        assert "ollama_chat_bounded(" in "\n".join(lines[start - 1:end]), name

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
    assert code_hits == [], f"app.py posts to /v1 at lines {code_hits}"


def test_native_tool_messages_converts_only_arguments_and_tool_name():
    transcript = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "thinking": "plan", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "web.search", "arguments": '{"query":"gas"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "system.info", "arguments": {"type": "all"}}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "name": "web.search", "content": "{}"},
    ]
    native = native_tool_messages(transcript)
    assert native[:2] == transcript[:2]
    calls = native[2]["tool_calls"]
    assert calls[0]["function"]["arguments"] == {"query": "gas"}  # string decoded
    assert calls[1]["function"]["arguments"] == {"type": "all"}   # object untouched
    assert calls[0]["id"] == "call_1" and native[2]["thinking"] == "plan"
    assert native[3] == {**transcript[3], "tool_name": "web.search"}
    assert transcript[2]["tool_calls"][0]["function"]["arguments"] == '{"query":"gas"}'  # input not mutated
    with pytest.raises(OllamaChatError):
        native_tool_messages([{"role": "assistant", "tool_calls": [{"function": {"name": "x", "arguments": "{bad"}}]}])


@pytest.mark.asyncio
async def test_bounded_chat_sends_native_tools_payload_and_caps_the_body():
    schema = {"type": "function", "function": {"name": "web.search", "parameters": {"type": "object"}}}
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{NODE_URL}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "ok"}, "done": True}))
        reply = await ollama_chat_bounded(
            NODE_URL, "m", [{"role": "user", "content": "q"}], tools=[schema], timeout=5, max_bytes=10_000,
            num_predict=64, temperature=0.2, options={"num_ctx": 16384}, keep_alive="30m",
        )
        assert reply["message"]["content"] == "ok"
        body = json.loads(route.calls.last.request.content)
        assert body["tools"] == [schema] and body["stream"] is False and body["keep_alive"] == "30m"
        assert body["options"] == {"top_p": 1.0, "num_predict": 64, "temperature": 0.2, "num_ctx": 16384}
        for openai_only in ("tool_choice", "max_tokens", "temperature"):
            assert openai_only not in body
        assert route.calls.last.request.headers["accept-encoding"] == "identity"

        route.mock(return_value=httpx.Response(200, content=b"x" * 5000))
        with pytest.raises(ValueError, match="byte limit"):
            await ollama_chat_bounded(NODE_URL, "m", [], tools=None, timeout=5, max_bytes=1000)
        route.mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(httpx.HTTPStatusError):
            await ollama_chat_bounded(NODE_URL, "m", [], tools=None, timeout=5, max_bytes=1000)


# DL-ROUTE-03: residency is a best-effort hint --------------------------------------------------

@pytest.mark.asyncio
async def test_loaded_models_reads_api_ps_and_never_raises():
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{NODE_URL}/api/ps")
        route.mock(return_value=httpx.Response(200, json={"models": [
            {"name": "llama3:latest", "model": "llama3:latest", "size_vram": 1},
            {"name": "gpt-oss:20b", "model": "gpt-oss:20b"}, "junk", {"name": 7},
        ]}))
        assert await ollama_loaded_models(NODE_URL + "/", timeout=1) == frozenset({"llama3:latest", "gpt-oss:20b"})
        route.mock(return_value=httpx.Response(200, json={"models": []}))
        assert await ollama_loaded_models(NODE_URL, timeout=1) == frozenset()
        for failure in (httpx.Response(500), httpx.Response(200, text="nope"), httpx.Response(200, json={"models": 3}),
                        httpx.ConnectError("down"), httpx.ReadTimeout("slow")):
            if isinstance(failure, Exception):
                route.mock(side_effect=failure)
            else:
                route.mock(return_value=failure)
            assert await ollama_loaded_models(NODE_URL, timeout=1) is None


def test_model_is_loaded_treats_a_bare_name_as_latest():
    loaded = {"llama3:latest", "gpt-oss:20b"}
    assert model_is_loaded("llama3", loaded) and model_is_loaded("llama3:latest", loaded)
    assert model_is_loaded("gpt-oss:20b", loaded)
    assert not model_is_loaded("gpt-oss:120b", loaded) and not model_is_loaded("gpt-oss", loaded)
    assert model_is_loaded("qwen3:latest", {"qwen3"})


class _DripPs(httpx.AsyncByteStream):
    """An /api/ps that answers headers at once and then drips its body."""

    async def __aiter__(self):
        for piece in (b'{"models"', b': []', b'}'):
            await asyncio.sleep(0.2)
            yield piece


@pytest.mark.asyncio
async def test_the_residency_probe_is_bounded_as_a_whole():
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{NODE_URL}/api/ps").mock(return_value=httpx.Response(200, stream=_DripPs()))
        started = time.monotonic()
        assert await ollama_loaded_models(NODE_URL, timeout=0.3) is None  # each read < 0.3 s, the whole > 0.3 s
        assert time.monotonic() - started < 0.55
        assert await ollama_loaded_models(NODE_URL, timeout=2) == frozenset()

