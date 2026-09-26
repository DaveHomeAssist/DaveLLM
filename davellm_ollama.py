"""Native Ollama ``POST /api/chat`` transport for plain chat (DL-TRANSPORT-01a).

DaveLLM's plain chat paths (``POST /chat``, ``POST /chat/stream`` and the
conversation summary) talk to a node through this module. It owns everything
that is specific to the native endpoint: the request payload, the per-message
base64 ``images`` list, NDJSON line parsing, the final-line timing metrics,
and two thin executors behind one entry point, ``ollama_chat(..., stream=...)``.

Why native and not ``/v1/chat/completions``: Ollama's OpenAI-compatible layer
ignores ``options.num_ctx`` and ``keep_alive`` (verified on Ollama 0.33.3). The
native endpoint honours both, streams ``message.content`` and
``message.thinking`` as separate fields, and reports timing on the ``done``
line. ``keep_alive`` and ``think`` default to ``None`` here, which means the
key is not sent at all; ``options`` defaults to ``None`` too, which adds
nothing beyond the ``num_predict``/``temperature`` keys the call sites pass
(each omitted when ``None``), so the node applies its own defaults for
everything else. The tool loop (``invoke_harness_model``, ``run_agent_endpoint``) stays on ``/v1``
because it exchanges tool schemas and OpenAI-shaped ``tool_calls``
(DL-TRANSPORT-01b).

Sampling note: ``/v1`` forced ``top_p`` to 1.0 on every request, and would
have forced ``temperature`` to 1.0 for a request that omitted or nulled it
(``app.py``'s ``chat_temperature`` keeps that substitution for the null case).
It never pinned the penalties: Ollama's ``/v1/chat/completions`` layer only
passes ``frequency_penalty``/``presence_penalty`` through when the request
supplies them, and DaveLLM never did. The native endpoint would apply the
model's Modelfile default for ``top_p``, so the payload builder sends 1.0 to
keep sampling identical; a caller can override it through ``options``.

The module is stateless, reads no environment, and never imports ``app``.
It raises only ``httpx`` exceptions plus the two module exceptions below, so
the call sites' existing ``except`` ladders keep producing today's strings.
"""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Literal, Mapping, Optional, Sequence, Union, overload

import httpx

OLLAMA_CHAT_PATH = "/api/chat"
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
) -> dict:
    """The ``/api/chat`` request body.

    ``num_predict`` and ``temperature`` become ``options`` keys (they were the
    OpenAI ``max_tokens`` and ``temperature``). ``top_p`` starts at the /v1
    value. Caller ``options`` are merged last so they win; ``None`` values
    inside them are dropped. ``keep_alive`` and ``think`` pass through verbatim
    and are omitted when ``None``.
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
    return payload


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
    )


class OllamaChatStream:
    """A streamed ``/api/chat`` reply: async context manager plus async iterator.

    No I/O happens until ``__aenter__``. Iteration yields one
    ``OllamaChatChunk`` per NDJSON line, accumulates ``content`` and
    ``thinking`` on the object, records ``done_reason`` and ``metrics`` from
    the ``done`` line, and stops there. A stream that ends without a ``done``
    line leaves ``completed`` false; the caller decides what that means.
    The context manager closes the response and the client, including on a
    UI abort in the middle of the stream.
    """

    def __init__(self, endpoint: str, payload: dict, timeout: float) -> None:
        self.endpoint = endpoint
        self.payload = payload
        self.timeout = timeout
        self.content = ""
        self.thinking = ""
        self.done_reason: Optional[str] = None
        self.metrics: Optional[OllamaMetrics] = None
        self.completed = False
        self._stack: Optional[AsyncExitStack] = None
        self._response: Optional[httpx.Response] = None

    async def __aenter__(self) -> "OllamaChatStream":
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=self.timeout))
            response = await stack.enter_async_context(
                client.stream("POST", self.endpoint, json=self.payload)
            )
            if not response.is_success:
                await response.aread()  # so .response.text / .content are available to the handler
                response.raise_for_status()
        except BaseException:
            await stack.aclose()  # __aexit__ never runs when __aenter__ raises
            raise
        self._stack = stack
        self._response = response
        return self

    async def __aexit__(self, *exc: object) -> None:
        stack, self._stack, self._response = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def __aiter__(self) -> AsyncIterator[OllamaChatChunk]:
        if self._response is None:
            raise RuntimeError("OllamaChatStream must be entered with 'async with' before iterating")
        async for line in self._response.aiter_lines():
            chunk = parse_ollama_chat_line(line)
            if chunk is None:
                continue
            self.content += chunk.content
            self.thinking += chunk.thinking
            if chunk.done:
                self.done_reason = chunk.done_reason
                self.metrics = chunk.metrics
                self.completed = True
            yield chunk
            if chunk.done:
                break


def _endpoint(node_url: str) -> str:
    return f"{node_url.rstrip('/')}{OLLAMA_CHAT_PATH}"


def ollama_chat_complete(
    node_url: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    timeout: float,
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
) -> OllamaChatResult:
    """One blocking ``/api/chat`` request on ``httpx.Client(timeout)``.

    Blocking by design: ``chat`` runs in FastAPI's threadpool and the summary
    is synchronous. From async code, wrap it in ``asyncio.to_thread`` (01b).
    Raises ``httpx.HTTPStatusError`` on non-2xx (body already read) and
    ``OllamaResponseError`` when a 2xx body is not a native chat response.
    """
    payload = build_ollama_chat_payload(
        model, messages, stream=False, num_predict=num_predict, temperature=temperature,
        options=options, keep_alive=keep_alive, think=think,
    )
    with httpx.Client(timeout=timeout) as client:
        response = client.post(_endpoint(node_url), json=payload)
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
    timeout: float,
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
) -> OllamaChatStream:
    """A not-yet-entered ``OllamaChatStream`` for ``async with``; no I/O here."""
    payload = build_ollama_chat_payload(
        model, messages, stream=True, num_predict=num_predict, temperature=temperature,
        options=options, keep_alive=keep_alive, think=think,
    )
    return OllamaChatStream(_endpoint(node_url), payload, timeout)


@overload
def ollama_chat(
    node_url: str, model: str, messages: Sequence[Mapping[str, Any]], *, stream: Literal[False],
    timeout: float, num_predict: Optional[int] = None, temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None, keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
) -> OllamaChatResult: ...


@overload
def ollama_chat(
    node_url: str, model: str, messages: Sequence[Mapping[str, Any]], *, stream: Literal[True],
    timeout: float, num_predict: Optional[int] = None, temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None, keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
) -> OllamaChatStream: ...


def ollama_chat(
    node_url: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    stream: bool,
    timeout: float,
    num_predict: Optional[int] = None,
    temperature: Optional[float] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[KeepAlive] = None,
    think: Optional[Think] = None,
) -> Union[OllamaChatResult, OllamaChatStream]:
    """The shared plain-chat entry point; dispatches on ``stream``.

    ``stream=False`` is ``ollama_chat_complete`` (blocking, returns the parsed
    reply). ``stream=True`` is ``ollama_chat_stream`` (returns an un-entered
    ``OllamaChatStream`` for ``async with ... as stream: async for chunk in stream``).
    ``node_url`` is a validated ``NodeConfig.url``; ``messages`` are already in
    native shape (see ``ollama_user_message``).
    """
    kwargs = dict(
        timeout=timeout, num_predict=num_predict, temperature=temperature,
        options=options, keep_alive=keep_alive, think=think,
    )
    if stream:
        return ollama_chat_stream(node_url, model, messages, **kwargs)
    return ollama_chat_complete(node_url, model, messages, **kwargs)
