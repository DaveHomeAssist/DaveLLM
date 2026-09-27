"""Exercise the real HTTPX connection path without making external requests."""
import socket
import ssl

import httpcore
import httpx
import pytest
import respx

from davellm_public_http import public_http_client, public_stream


class WireStream(httpcore.AsyncNetworkStream):
    def __init__(self, body=b"public page", *, status=b"200 OK", headers=b""):
        self.response = (b"HTTP/1.1 " + status + b"\r\nContent-Length: " + str(len(body)).encode()
                         + b"\r\nContent-Type: text/plain\r\n" + headers + b"\r\n" + body)
        self.writes = []
        self.tls = []
        self.closed = False

    async def read(self, max_bytes, timeout=None):
        response, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return response

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
        return self

    def get_extra_info(self, info):
        return False if info == "is_readable" else None


def dns_answer(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (address, port))


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_fetch_pins_dns_and_preserves_host_and_tls_identity(router_factory, monkeypatch, scheme):
    router, _, _ = router_factory(tools=True)
    # An environment proxy must never receive or independently resolve a public-tool URL.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    lookups, connections = [], []
    wire = WireStream()

    def dns(host, port, **kwargs):
        lookups.append(host)
        return [dns_answer("93.184.216.34" if len(lookups) == 1 else "127.0.0.1", port)]

    async def connect(self, host, port, **kwargs):
        connections.append((host, port))
        return wire

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    result = await router.tool_web_fetch({"url": f"{scheme}://rebind.test:8443/page"})
    assert result.status == "success", result
    assert result.result == "public page"
    assert lookups == ["rebind.test"]
    assert connections == [("93.184.216.34", 8443)]
    assert b"Host: rebind.test:8443\r\n" in b"".join(wire.writes)
    assert wire.tls == ([("rebind.test", True, ssl.CERT_REQUIRED)] if scheme == "https" else [])
    assert wire.closed


@pytest.mark.asyncio
async def test_redirect_revalidates_same_hostname_before_another_connection(router_factory, monkeypatch):
    router, _, _ = router_factory(tools=True)
    lookups, connections = [], []
    wire = WireStream(status=b"302 Found", headers=b"Location: /private\r\n")

    def dns(host, port, **kwargs):
        lookups.append(host)
        return [dns_answer("93.184.216.34" if len(lookups) == 1 else "127.0.0.1", port)]

    async def connect(self, host, port, **kwargs):
        connections.append(host)
        return wire

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    result = await router.tool_web_fetch({"url": "http://rebind.test/public"})
    assert result.status == "error"
    assert "not public" in result.error
    assert lookups == ["rebind.test", "rebind.test"]
    assert connections == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answers_never_connect(router_factory, monkeypatch):
    router, _, _ = router_factory(tools=True)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [
        dns_answer("93.184.216.34", port), dns_answer("100.64.0.1", port),
    ])

    async def forbidden(*args, **kwargs):
        pytest.fail("mixed DNS answers must be refused before connecting")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    result = await router.tool_web_fetch({"url": "http://mixed.test/page"})
    assert result.status == "error"
    assert "not public" in result.error


@pytest.mark.asyncio
async def test_public_stream_retries_only_vetted_addresses_on_connect_failure():
    addresses = ("2606:4700:4700::1111", "93.184.216.34")
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://[2606:4700:4700::1111]/page").mock(side_effect=httpx.ConnectError("offline"))
        final = mock.get("https://93.184.216.34/page").mock(return_value=httpx.Response(200, text="ok"))
        async with public_http_client(timeout=10) as client:
            async with public_stream(client, "https://public.test/page", addresses) as response:
                assert await response.aread() == b"ok"
    assert final.calls.last.request.headers["Host"] == "public.test"
    assert final.calls.last.request.extensions["sni_hostname"] == "public.test"


@pytest.mark.asyncio
async def test_shared_ip_does_not_reuse_another_hosts_tls_connection(monkeypatch):
    wires = []

    async def connect(self, host, port, **kwargs):
        assert host == "93.184.216.34"
        wire = WireStream()
        wires.append(wire)
        return wire

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with public_http_client(timeout=10) as client:
        for host in ("one.test", "two.test"):
            async with public_stream(client, f"https://{host}/", ("93.184.216.34",)) as response:
                assert await response.aread() == b"public page"
    assert len(wires) == 2
    assert [wire.tls[0][0] for wire in wires] == ["one.test", "two.test"]
    assert all(wire.closed for wire in wires)
