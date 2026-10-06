"""Native Ollama ``POST /api/chat`` transport for plain chat and the tool loop (DL-TRANSPORT-01a/01b).

DaveLLM's plain chat paths (``POST /chat``, ``POST /chat/stream`` and the
conversation summary) talk to a node through this module. It owns everything
that is specific to the native endpoint: the request payload, the per-message
base64 ``images`` list, NDJSON line parsing, the final-line timing metrics,
and two thin executors behind one entry point, ``ollama_chat(..., stream=...)``.

Plain chat sends no ``tools``, but a model can still reply with only a tool
call (gpt-oss does, with a hallucinated ``browser.*`` call and empty content).
The parsers keep ``message.tool_calls`` as-is on every chunk and result so the
router can tell that apart from an empty reply; nothing here runs a tool.

Why native and not ``/v1/chat/completions``: Ollama's OpenAI-compatible layer
ignores ``options.num_ctx`` and ``keep_alive`` (verified on Ollama 0.33.3). The
native endpoint honours both, streams ``message.content`` and
``message.thinking`` as separate fields, and reports timing on the ``done``
line. ``keep_alive`` and ``think`` default to ``None`` here, which means the
key is not sent at all; the router's hooks supply ``num_ctx`` and
``keep_alive`` (DL-CTX-01, DL-KEEP-01).

The tool loop (``invoke_harness_model``, ``run_agent_endpoint``) uses
``ollama_chat_bounded`` (DL-TRANSPORT-01b): it sends the tool schemas, reads the
reply with a byte cap, and returns the native response, whose top-level
``message`` DaveHarness reads directly (native tool calls carry an ``id`` and
object ``arguments``). ``native_tool_messages`` converts the only two fields
DaveHarness writes differently on the way in: string arguments and ``name``
on tool results.

Sampling note: ``/v1`` forced ``top_p`` to 1.0 on every request, and would
have forced ``temperature`` to 1.0 for a request that omitted or nulled it
(``app.py``'s ``chat_temperature`` keeps that substitution for the null case).
It never pinned the penalties: Ollama's ``/v1/chat/completions`` layer only
passes ``frequency_penalty``/``presence_penalty`` through when the request
supplies them, and DaveLLM never did. The native endpoint would apply the
model's Modelfile default for ``top_p``, so the payload builder sends 1.0 to
keep sampling identical; a caller can override it through ``options``.

``ollama_loaded_models`` (DL-ROUTE-03) reads ``GET /api/ps`` as a best-effort hint
and never raises.

The module is stateless, reads no environment, and never imports ``app``.
It raises only ``httpx`` exceptions, ``ValueError`` from the tool-loop byte cap,
plus the module exceptions below, so
the call sites' existing ``except`` ladders keep producing today's strings.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Literal, Mapping, Optional, Sequence, Union, overload

import httpx

OLLAMA_CHAT_PATH = "/api/chat"
OLLAMA_PS_PATH = "/api/ps"
# Media-type parameters are allowed before ";base64," (RFC 2397), e.g. ";charset=utf-8".
_DATA_URL_PREFIX = re.compile(r"^data:[^,]*;base64,", re.IGNORECASE)
# /v1 sent top_p 1.0 on every request; keep that so the transport swap does not change sampling.
V1_TOP_P = 1.0
_METRIC_KEYS = (
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
    "prompt_eval_cached_count",
)

KeepAlive = Union[str, int, float]
Think = Union[bool, str]


class OllamaChatError(Exception):
    """Base class for the two module-level failures."""


class OllamaResponseError(OllamaChatError):
    """A 2xx body that is not a native chat response (non-stream mode)."""


class OllamaStreamError(OllamaChatError):
    """An in-band ``{"error": "..."}`` NDJSON line (stream mode)."""


@dataclass(frozen=True)
class OllamaMetrics:
    """Timing and token counts from the ``done`` line; durations in nanoseconds.

    Every field is optional because Ollama omits zero values, and
    ``prompt_eval_cached_count`` only exists on 0.33 and later.
    """

    total_duration: Optional[int] = None
    load_duration: Optional[int] = None
    prompt_eval_count: Optional[int] = None
    prompt_eval_duration: Optional[int] = None
    eval_count: Optional[int] = None
    eval_duration: Optional[int] = None
    prompt_eval_cached_count: Optional[int] = None

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "OllamaMetrics":
        values = {}
        for key in _METRIC_KEYS:
            value = data.get(key)
            values[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
        return cls(**values)


@dataclass(frozen=True)
class OllamaChatChunk:
    """One NDJSON line of a streamed reply."""

    content: str
    thinking: str
    done: bool
    done_reason: Optional[str]
    metrics: Optional[OllamaMetrics]
    raw: dict
    tool_calls: tuple[dict, ...] = ()


@dataclass(frozen=True)
class OllamaChatResult:
    """A non-stream reply."""

    content: str
    thinking: str
    done_reason: Optional[str]
    metrics: OllamaMetrics
    model: Optional[str]
    created_at: Optional[str]
    raw: dict
    tool_calls: tuple[dict, ...] = ()


def _message_tool_calls(message: Mapping[str, Any]) -> tuple[dict, ...]:
    """The object entries of ``message.tool_calls``; anything else is ignored, never raised."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return ()
    return tuple(call for call in calls if isinstance(call, dict))


def ollama_tool_call_names(tool_calls: Iterable[Mapping[str, Any]]) -> list[str]:
    """The ``function.name`` of each native tool call, in order; calls without a string name are skipped."""
    names = []
    for call in tool_calls:
        function = call.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str):
            names.append(name)
    return names


def ollama_image_data(images: Iterable[str]) -> list[str]:
    """Return the raw base64 of each image, without any ``data:<type>;base64,`` prefix.

    The UI sends data URLs (``FileReader.readAsDataURL``); Ollama's ``images``
    list wants the bare, padded base64 and rejects a leftover prefix.
    """
    return [_DATA_URL_PREFIX.sub("", str(image).strip(), count=1) for image in images]


def ollama_user_message(text: str, images: Sequence[str]) -> dict:
    """A native user message; ``images`` is omitted when there are none."""
    message: dict = {"role": "user", "content": text}
    if images:
        message["images"] = list(images)
    return message


def build_ollama_chat_payload(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    stream: bool,
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
    tools: Optional[Sequence[Mapping[str, Any]]] = None,
    format: Optional[str] = None,
) -> dict:
    """The ``/api/chat`` request body.

    ``num_predict`` and ``temperature`` become ``options`` keys (they were the
    OpenAI ``max_tokens`` and ``temperature``). ``top_p`` starts at the /v1
    value. Caller ``options`` are merged last so they win; ``None`` values
    inside them are dropped. ``keep_alive`` and ``think`` pass through verbatim
    and are omitted when ``None``; ``tools`` (function schemas, the same shape
    as OpenAI's) is sent only when non-empty. ``format`` (``"json"``) becomes
    Ollama's top-level ``format`` field and is omitted when ``None``.
    """
    payload: dict = {"model": model, "messages": list(messages), "stream": bool(stream)}
    opts: dict = {"top_p": V1_TOP_P}
    if num_predict is not None:
        opts["num_predict"] = num_predict
    if temperature is not None:
        opts["temperature"] = temperature
    if options:
        opts.update({key: value for key, value in options.items() if value is not None})
    if opts:
        payload["options"] = opts
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if think is not None:
        payload["think"] = think
    if tools:
        payload["tools"] = [dict(tool) for tool in tools]
    if format is not None:
        payload["format"] = format
    return payload


def native_tool_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict]:
    """A DaveHarness transcript in the shape native ``/api/chat`` accepts.

    DaveHarness writes tool calls OpenAI-style: ``function.arguments`` is a
    JSON string and tool results name their tool under ``name``. Native
    ``/api/chat`` rejects string arguments (HTTP 400) and reads the tool name
    from ``tool_name``, which gpt-oss's template needs to place the result.
    Only those two fields change; everything else passes through.
    """
    converted: list[dict] = []
    for message in messages:
        item = dict(message)
        calls = item.get("tool_calls")
        if item.get("role") == "assistant" and isinstance(calls, list):
            native_calls = []
            for call in calls:
                call = dict(call) if isinstance(call, Mapping) else call
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, Mapping) and isinstance(function.get("arguments"), str):
                    function = dict(function)
                    try:
                        function["arguments"] = json.loads(function["arguments"] or "{}")
                    except ValueError as exc:
                        raise OllamaChatError("Tool call arguments are not valid JSON") from exc
                    call["function"] = function
                native_calls.append(call)
            item["tool_calls"] = native_calls
        if item.get("role") == "tool" and "tool_name" not in item and isinstance(item.get("name"), str):
            item["tool_name"] = item["name"]
        converted.append(item)
    return converted


async def ollama_chat_bounded(
    node_url: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Optional[Sequence[Mapping[str, Any]]],
    timeout: Union[float, httpx.Timeout],
    max_bytes: int,
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
) -> dict:
    """One non-streaming ``/api/chat`` call for the tool loop, read with a byte cap.

    Returns the native response object unchanged (DaveHarness reads its top-level
    ``message``). Raises ``httpx.HTTPStatusError`` on non-2xx and ``ValueError``
    when the body exceeds ``max_bytes`` or is not JSON.
    """
    payload = build_ollama_chat_payload(
        model, native_tool_messages(messages), stream=False, num_predict=num_predict,
        temperature=temperature, options=options, keep_alive=keep_alive, tools=tools,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", _endpoint(node_url), json=payload, headers={"Accept-Encoding": "identity"},
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_raw(chunk_size=65_536):
                if len(body) + len(chunk) > max_bytes:
                    raise ValueError("Model response exceeds the tool run byte limit")
                body.extend(chunk)
    return json.loads(body)


def parse_ollama_chat_line(line: str) -> Optional[OllamaChatChunk]:
    """Parse one NDJSON line; ``None`` for blank, non-JSON or non-object lines.

    Raises ``OllamaStreamError`` for an in-band ``{"error": ...}`` line.
    Content and thinking are read from every line, including the ``done``
    line, because Ollama may put the last token there.
    """
    text = line.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "error" in data:
        raise OllamaStreamError(str(data["error"]))
    message = data.get("message") or {}
    if not isinstance(message, Mapping):
        message = {}
    done = bool(data.get("done"))
    return OllamaChatChunk(
        content=message.get("content") or "",
        thinking=message.get("thinking") or "",
        done=done,
        done_reason=data.get("done_reason") if done else None,
        metrics=OllamaMetrics.from_payload(data) if done else None,
        raw=data,
        tool_calls=_message_tool_calls(message),
    )


def parse_ollama_chat_response(data: object) -> OllamaChatResult:
    """Parse a non-stream body; ``OllamaResponseError`` when it is not native."""
    try:
        if not isinstance(data, dict):
            raise TypeError(f"expected a JSON object, got {type(data).__name__}")
        if "error" in data:
            raise ValueError(str(data["error"]))
        message = data["message"]
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError("message.content is not a string")
        thinking = message.get("thinking") or ""
        if not isinstance(thinking, str):
            raise TypeError("message.thinking is not a string")
    except (KeyError, TypeError, ValueError) as exc:
        raise OllamaResponseError(str(exc)) from exc
    return OllamaChatResult(
        content=content,
        thinking=thinking,
        done_reason=data.get("done_reason"),
        metrics=OllamaMetrics.from_payload(data),
        model=data.get("model"),
        created_at=data.get("created_at"),
        raw=data,
        tool_calls=_message_tool_calls(message),
    )


class OllamaChatStream:
    """A streamed ``/api/chat`` reply: async context manager plus async iterator.

    No I/O happens until ``__aenter__``. Iteration yields one
    ``OllamaChatChunk`` per NDJSON line, accumulates ``content``,
    ``thinking`` and ``tool_calls`` on the object, records ``done_reason`` and ``metrics`` from
    the ``done`` line, and stops there. A stream that ends without a ``done``
    line leaves ``completed`` false; the caller decides what that means.
    The context manager closes the response and the client, including on a
    UI abort in the middle of the stream.

    DL-TIME-01: ``first_chunk_timeout`` bounds the wait from the request start
    to the first NDJSON line (model load plus prompt reading), and
    ``idle_timeout`` bounds each gap after that. Either one expiring raises
    ``httpx.ReadTimeout``, the same exception an httpx read timeout raises, so
    callers keep their existing handling. ``None`` leaves that deadline off.
    A non-2xx reply's error body is read within the first-chunk budget too.
    """

    def __init__(
        self,
        endpoint: str,
        payload: dict,
        timeout: Union[float, httpx.Timeout],
        *,
        first_chunk_timeout: Optional[float] = None,
        idle_timeout: Optional[float] = None,
    ) -> None:
        self.endpoint = endpoint
        self.payload = payload
        self.timeout = timeout
        self.first_chunk_timeout = first_chunk_timeout
        self.idle_timeout = idle_timeout
        self._started: Optional[float] = None
        self.content = ""
        self.thinking = ""
        self.tool_calls: list[dict] = []
        self.done_reason: Optional[str] = None
        self.metrics: Optional[OllamaMetrics] = None
        self.completed = False
        self._stack: Optional[AsyncExitStack] = None
        self._response: Optional[httpx.Response] = None

    async def __aenter__(self) -> "OllamaChatStream":
        stack = AsyncExitStack()
        await stack.__aenter__()
        self._started = asyncio.get_running_loop().time()
        try:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=self.timeout))
            try:
                response = await asyncio.wait_for(
                    stack.enter_async_context(client.stream("POST", self.endpoint, json=self.payload)),
                    self.first_chunk_timeout,
                )
            except asyncio.TimeoutError:
                raise httpx.ReadTimeout("No response from the node before the first-chunk deadline") from None
            if not response.is_success:
                # Read the error body (so .response.text is available to the handler) within the same
                # first-chunk budget: a node that sends error headers and then stalls must still time out.
                try:
                    await asyncio.wait_for(response.aread(), self._first_chunk_wait())
                except asyncio.TimeoutError:
                    raise httpx.ReadTimeout("The node stopped sending its error before the first-chunk deadline") from None
                response.raise_for_status()
        except BaseException:
            await stack.aclose()  # __aexit__ never runs when __aenter__ raises
            raise
        self._stack = stack
        self._response = response
        return self

    def _first_chunk_wait(self) -> Optional[float]:
        """What is left of the first-chunk budget; the idle budget when there is none."""
        if self.first_chunk_timeout is None:
            return self.idle_timeout
        elapsed = asyncio.get_running_loop().time() - (self._started or 0.0)
        return max(0.0, self.first_chunk_timeout - elapsed)

    async def __aexit__(self, *exc: object) -> None:
        stack, self._stack, self._response = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def __aiter__(self) -> AsyncIterator[OllamaChatChunk]:
        if self._response is None:
            raise RuntimeError("OllamaChatStream must be entered with 'async with' before iterating")
        lines = self._response.aiter_lines()
        first = True
        while True:
            wait = self._first_chunk_wait() if first else self.idle_timeout
            try:
                line = await asyncio.wait_for(lines.__anext__(), wait)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                which = "first-chunk" if first else "idle"
                raise httpx.ReadTimeout(f"The node stopped sending before the {which} deadline") from None
            first = False
            chunk = parse_ollama_chat_line(line)
            if chunk is None:
                continue
            self.content += chunk.content
            self.thinking += chunk.thinking
            self.tool_calls.extend(chunk.tool_calls)
            if chunk.done:
                self.done_reason = chunk.done_reason
                self.metrics = chunk.metrics
                self.completed = True
            yield chunk
            if chunk.done:
                break


async def ollama_loaded_models(node_url: str, *, timeout: float) -> Optional[frozenset[str]]:
    """Names of the models the node holds in memory (``GET /api/ps``); ``None`` when unknown.

    DL-ROUTE-03: residency is only a hint for the UI, so every failure (unreachable,
    slow, non-2xx, unexpected body) is ``None`` and nothing is raised. ``timeout`` is
    the wall clock for the whole probe, not just each read.
    """
    async def probe() -> Any:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{node_url.rstrip('/')}{OLLAMA_PS_PATH}")
        return response.json().get("models") if response.is_success else None

    try:
        # httpx timeouts are per operation; the wall clock bounds the whole probe.
        models = await asyncio.wait_for(probe(), timeout)
    except Exception:  # asyncio.TimeoutError included
        return None
    if not isinstance(models, list):
        return None
    return frozenset(
        item[key] for item in models if isinstance(item, dict)
        for key in ("name", "model") if isinstance(item.get(key), str)
    )


def model_is_loaded(model_id: str, loaded: Iterable[str]) -> bool:
    """Whether ``model_id`` is among ``loaded``; ``llama3`` and ``llama3:latest`` are one model."""
    def canonical(name: str) -> str:
        return name if ":" in name else f"{name}:latest"
    return canonical(model_id) in {canonical(name) for name in loaded}


def _endpoint(node_url: str) -> str:
    return f"{node_url.rstrip('/')}{OLLAMA_CHAT_PATH}"


def ollama_chat_complete(
    node_url: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    timeout: Union[float, httpx.Timeout],
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
    total_timeout: Optional[float] = None,
    format: Optional[str] = None,
) -> OllamaChatResult:
    """One blocking ``/api/chat`` request, cancellable when a total is set.

    Blocking by design: ``chat`` runs in FastAPI's threadpool and the summary
    is synchronous. From async code, wrap it in ``asyncio.to_thread`` (01b).
    Raises ``httpx.HTTPStatusError`` on non-2xx (body already read) and
    ``OllamaResponseError`` when a 2xx body is not a native chat response.

    httpx timeouts are per operation, so ``timeout`` alone lets a node that keeps
    sending a few bytes at a time run forever. ``total_timeout`` (seconds) is the
    wall clock for the whole reply, including headers and body completion.
    One async deadline cancels an in-progress read; it does not restart when
    bytes arrive or leave a blocking request running in a background thread.
    """
    payload = build_ollama_chat_payload(
        model, messages, stream=False, num_predict=num_predict, temperature=temperature,
        options=options, keep_alive=keep_alive, think=think, format=format,
    )
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # post() includes the entire body read, even for a non-2xx response.
            return await client.post(
                _endpoint(node_url), json=payload, headers={"Accept-Encoding": "identity"},
            )

    async def complete() -> httpx.Response:
        try:
            return await asyncio.wait_for(request(), total_timeout)
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout(
                "The node did not finish its reply before the total deadline",
                request=httpx.Request("POST", _endpoint(node_url)),
            ) from None

    if total_timeout is None:
        # The background summarizer calls this synchronous path from its event
        # loop. Preserve that path; asyncio.run() is only for deadline-bound chat.
        with httpx.Client(timeout=timeout) as client:
            response = client.post(_endpoint(node_url), json=payload, headers={"Accept-Encoding": "identity"})
    else:
        response = asyncio.run(complete())
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise OllamaResponseError(str(exc)) from exc
    return parse_ollama_chat_response(data)


def ollama_chat_stream(
    node_url: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    timeout: Union[float, httpx.Timeout],
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
    first_chunk_timeout: Optional[float] = None,
    idle_timeout: Optional[float] = None,
) -> OllamaChatStream:
    """A not-yet-entered ``OllamaChatStream`` for ``async with``; no I/O here."""
    payload = build_ollama_chat_payload(
        model, messages, stream=True, num_predict=num_predict, temperature=temperature,
        options=options, keep_alive=keep_alive, think=think,
    )
    return OllamaChatStream(
        _endpoint(node_url), payload, timeout,
        first_chunk_timeout=first_chunk_timeout, idle_timeout=idle_timeout,
    )


@overload
def ollama_chat(
    node_url: str, model: str, messages: Sequence[Mapping[str, Any]], *, stream: Literal[False],
    timeout: Union[float, httpx.Timeout], num_predict: Optional[int] = None, temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None, keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None, total_timeout: Optional[float] = None, format: Optional[str] = None,
) -> OllamaChatResult: ...


@overload
def ollama_chat(
    node_url: str, model: str, messages: Sequence[Mapping[str, Any]], *, stream: Literal[True],
    timeout: Union[float, httpx.Timeout], num_predict: Optional[int] = None, temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None, keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None, first_chunk_timeout: Optional[float] = None,
    idle_timeout: Optional[float] = None,
) -> OllamaChatStream: ...


def ollama_chat(
    node_url: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    stream: bool,
    timeout: Union[float, httpx.Timeout],
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
    first_chunk_timeout: Optional[float] = None,
    idle_timeout: Optional[float] = None,
    total_timeout: Optional[float] = None,
    format: Optional[str] = None,
) -> Union[OllamaChatResult, OllamaChatStream]:
    """The shared plain-chat entry point; dispatches on ``stream``.

    ``stream=False`` is ``ollama_chat_complete`` (blocking, returns the parsed
    reply). ``stream=True`` is ``ollama_chat_stream`` (returns an un-entered
    ``OllamaChatStream`` for ``async with ... as stream: async for chunk in stream``).
    ``node_url`` is a validated ``NodeConfig.url``; ``messages`` are already in
    native shape (see ``ollama_user_message``). ``timeout`` is a number or an
    ``httpx.Timeout``; the stream-only ``first_chunk_timeout`` / ``idle_timeout``
    are ignored for a complete (non-streaming) call, and the complete-only
    ``total_timeout`` (wall clock for the whole reply) and ``format`` are
    ignored for a stream.
    """
    kwargs = dict(
        timeout=timeout, num_predict=num_predict, temperature=temperature,
        options=options, keep_alive=keep_alive, think=think,
    )
    if stream:
        return ollama_chat_stream(
            node_url, model, messages, **kwargs,
            first_chunk_timeout=first_chunk_timeout, idle_timeout=idle_timeout,
        )
    return ollama_chat_complete(node_url, model, messages, **kwargs, total_timeout=total_timeout, format=format)
