"""web.search (private SearXNG) and web.read (readable public pages), both extended tools."""

import socket

import httpx
import pytest
import respx

from conftest import TEST_API_KEY
from davellm_web import html_to_text, normalize_search_url

AUTH = {"X-API-Key": TEST_API_KEY}
SEARCH_URL = "http://search.test:8890"


@pytest.fixture
def web(router_factory, monkeypatch):
    """App with tools, extended tools and a search URL; DNS answers public for *.test hosts."""
    def load(search_url=SEARCH_URL):
        monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", "true")
        if search_url is None:
            monkeypatch.delenv("DAVE_SEARCH_URL", raising=False)
        else:
            monkeypatch.setenv("DAVE_SEARCH_URL", search_url)
        router, client, _ = router_factory(tools=True)

        def public_dns(host, *_args, **_kwargs):
            address = "127.0.0.1" if host in {"127.0.0.1", "localhost"} else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

        monkeypatch.setattr(router.socket, "getaddrinfo", public_dns)
        return router, client
    return load


def run(client, tool, **params):
    return client.post("/tools/execute", headers=AUTH, json={"tool": tool, "params": params}).json()


def searx(*results):
    return httpx.Response(200, json={"query": "q", "results": list(results)})


def test_html_to_text_keeps_visible_text_and_drops_code():
    html = (
        "<html><head><title>Fuel &amp; prices</title><style>.x{color:red}</style>"
        "<script>var price = 9.99;</script></head><body><nav>Home</nav>"
        "<h1>U.S. Regular</h1><p>National   average:<br>$4.478</p>"
        "<noscript>enable js</noscript><svg><text>logo</text></svg><ul><li>East</li><li>West</li></ul></body></html>"
    )
    assert html_to_text(html) == "Fuel & prices\nHome\nU.S. Regular\nNational average:\n$4.478\nEast\nWest"


@pytest.mark.parametrize("separator", ["", "\n  "])
def test_html_to_text_keeps_table_headers_and_prices_separate(separator):
    cells = separator.join(["<td>4.157</td>", "<td><strong>4.319</strong></td>", "<td>4.478</td>"])
    html = ("<table><tr><th>Regular</th><th>Midgrade</th><th>Premium</th></tr>"
            f"<tr>{cells}</tr></table>")
    result = html_to_text(html)
    assert result.split() == ["Regular", "Midgrade", "Premium", "4.157", "4.319", "4.478"]
    assert "RegularMidgrade" not in result
    assert "4.1574.319" not in result


def test_search_url_must_be_http_or_https():
    assert normalize_search_url("http://search.test:8890/") == "http://search.test:8890"
    assert normalize_search_url(" https://s.test ") == "https://s.test"
    for bad in (None, "", "  ", "ftp://s.test", "search.test:8890", "http://"):
        assert normalize_search_url(bad) is None


def test_web_search_formats_deduplicates_and_caps_results(web):
    _, client = web()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{SEARCH_URL}/search").mock(return_value=searx(
            {"title": "AAA   Fuel\nPrices", "url": "https://gasprices.aaa.com/", "content": "National average " * 40},
            {"title": "dup", "url": "https://gasprices.aaa.com/", "content": "same url again"},
            {"title": "not web", "url": "javascript:alert(1)", "content": "x"},
            {"title": "", "url": "https://www.eia.gov/petroleum/gasdiesel/", "content": ""},
            {"title": "third", "url": "https://example.test/3", "content": "c"},
        ))
        out = run(client, "web.search", query="average gasoline price", max_results=2)

    assert out["status"] == "success", out
    request = route.calls.last.request
    assert request.url.params["q"] == "average gasoline price"
    assert request.url.params["format"] == "json"
    assert request.url.params["safesearch"] == "1"
    text = out["result"]
    assert text.startswith('Results for "average gasoline price":')
    assert "1. AAA Fuel Prices\n   https://gasprices.aaa.com/\n   National average" in text
    assert "2. https://www.eia.gov/petroleum/gasdiesel/\n   https://www.eia.gov/petroleum/gasdiesel/" in text
    assert "javascript:" not in text and "same url again" not in text and "third" not in text
    first_entry = text.split("\n\n")[1]
    assert len(first_entry.split("\n   ")[2]) == 300  # snippet clipped


def test_web_search_defaults_to_five_results_and_reports_none(web):
    _, client = web()
    many = [{"title": f"r{i}", "url": f"https://example.test/{i}", "content": ""} for i in range(8)]
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{SEARCH_URL}/search")
        route.mock(return_value=searx(*many))
        assert run(client, "web.search", query="x")["result"].count("https://example.test/") == 5
        route.mock(return_value=searx())
        assert run(client, "web.search", query="nothing here")["result"] == 'No results for "nothing here".'


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(500, text="boom"), "The search service returned HTTP 500"),
        (httpx.Response(200, text="<html>not json</html>"), "The search service returned an invalid response"),
        (httpx.ConnectTimeout("slow"), "The search service timed out"),
        (httpx.ConnectError("refused"), "The search service could not be reached"),
    ],
)
def test_web_search_errors_are_fixed_messages(web, response, message):
    _, client = web()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{SEARCH_URL}/search")
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        out = run(client, "web.search", query="q")
    assert out["status"] == "error"
    assert out["error"] == message


def test_web_search_without_a_search_url_says_so(web):
    _, client = web(search_url=None)
    out = run(client, "web.search", query="q")
    assert out["status"] == "error"
    assert "DAVE_SEARCH_URL is unset" in out["error"]


def test_web_read_returns_visible_text_not_markup(web):
    _, client = web()
    page = "<html><head><script>" + "x" * 20000 + "</script></head><body><p>U.S. regular $4.478</p></body></html>"
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://93.184.216.34/prices").mock(
            return_value=httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
        )
        out = run(client, "web.read", url="https://fuel.test/prices")
    assert out["status"] == "success", out
    assert out["result"] == "U.S. regular $4.478"


def test_web_read_follows_public_redirects_and_names_the_final_page(web):
    _, client = web()
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://93.184.216.34/old").mock(return_value=httpx.Response(301, headers={"location": "/new"}))
        mock.get("https://93.184.216.34/new").mock(
            return_value=httpx.Response(200, text="plain body", headers={"content-type": "text/plain"})
        )
        out = run(client, "web.read", url="https://fuel.test/old")
    assert out["status"] == "success", out
    assert out["result"] == "https://fuel.test/new\n\nplain body"


def test_web_read_refuses_private_addresses_on_every_hop(web):
    _, client = web()
    assert "not public" in run(client, "web.read", url="http://127.0.0.1/admin")["error"]
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://93.184.216.34/hop").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        )
        private = mock.get("http://127.0.0.1/private")
        out = run(client, "web.read", url="https://fuel.test/hop")
    assert out["status"] == "error"
    assert "not public" in out["error"]
    assert not private.called


def test_web_read_limits_content_type_size_and_output(web):
    router, client = web()
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://93.184.216.34/pdf").mock(
            return_value=httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        )
        mock.get("https://93.184.216.34/huge").mock(
            return_value=httpx.Response(
                200, content=b"a" * (router.MAX_WEB_FETCH_BYTES + 1), headers={"content-type": "text/plain"}
            )
        )
        mock.get("https://93.184.216.34/long").mock(
            return_value=httpx.Response(200, text="word " * 5000, headers={"content-type": "text/plain"})
        )
        mock.get("https://93.184.216.34/gone").mock(return_value=httpx.Response(404))
        assert run(client, "web.read", url="https://fuel.test/pdf")["error"] == "Unsupported content type: application/pdf"
        assert "byte limit" in run(client, "web.read", url="https://fuel.test/huge")["error"]
        assert run(client, "web.read", url="https://fuel.test/gone")["error"] == "The page returned HTTP 404"
        long = run(client, "web.read", url="https://fuel.test/long")["result"]
    assert len(long) == router.MAX_TOOL_OUTPUT
    assert long.endswith(f"[truncated: showing {router.MAX_TOOL_OUTPUT} of 25000 characters]")
