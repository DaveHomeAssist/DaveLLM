"""Connect public web tools only to addresses admitted by their DNS validator.

The original URL supplies Host and TLS SNI/certificate identity; only the TCP
destination changes. Environment proxies are disabled because they could resolve
the hostname independently. Connections are not pooled across requests, avoiding
reuse of one site's TLS connection for another hostname sharing the same IP.
"""

from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator, Sequence

import httpx


def public_http_client(*, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
        limits=httpx.Limits(max_keepalive_connections=0),
    )


@asynccontextmanager
async def public_stream(
    client: httpx.AsyncClient, url: str, addresses: Sequence[str],
) -> AsyncIterator[httpx.Response]:
    """Stream a GET to vetted numeric addresses, preserving the URL's identity.

    Retry another vetted address only if connection establishment fails. Never
    re-resolve the original hostname or retry after receiving a response.
    """
    if not addresses:
        raise ValueError("Hostname did not resolve")
    original = httpx.Request("GET", url)
    for index, address in enumerate(addresses):
        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(client.stream(
                "GET", original.url.copy_with(host=address),
                headers={"Host": original.headers["Host"]},
                extensions={"sni_hostname": original.url.host},
            ))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            await stack.aclose()
            if index == len(addresses) - 1:
                raise
            continue
        async with stack:
            yield response
        return
