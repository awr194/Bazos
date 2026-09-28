"""Тесты разведчика Sbazar без сети (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx

from src import probe_sbazar as probe
from src.scraper import BazosClient

HOME_HTML = """
<html><head>
<script src="/static/app.js"></script>
<script id="__NEXT_DATA__" type="application/json">{"props": {"items": [{"id": 1, "name": "iPhone", "price": 5000}]}}</script>
<script>window.__STATE__ = {"categories": [{"id": 30, "seo_name": "elektro"}]};
</script>
</head><body>
<a href="/hledej/iphone">Hledat</a>
<a href="https://www.sbazar.cz/uzivatel/detail/123-iphone-13">iPhone 13</a>
<a href="https://www.seznam.cz/">Seznam</a>
<a href="/soukrome/">Zakázáno</a>
</body></html>
"""
SEARCH_HTML = '<html><body><a href="/prodejce/detail/456-iphone-12">iPhone 12</a></body></html>'
DETAIL_HTML = "<html><body><h1>iPhone 12</h1><p>Cena: 4 000 Kč</p><p>Zobrazení: 57</p></body></html>"
BUNDLE_JS = 'fetch("/api/v1/items/search?phrase="+q); const c = "/api/v1/categories";'
ROBOTS = "User-agent: *\nDisallow: /soukrome/\nDisallow: /api/v1/categories/tree\n"


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(200, text=ROBOTS, headers={"content-type": "text/plain"})
    if path == "/":
        return httpx.Response(200, text=HOME_HTML, headers={"content-type": "text/html"})
    if path == "/static/app.js":
        return httpx.Response(200, text=BUNDLE_JS, headers={"content-type": "application/javascript"})
    if path.startswith("/hledej/"):
        return httpx.Response(200, text=SEARCH_HTML, headers={"content-type": "text/html"})
    if "/detail/" in path:
        return httpx.Response(200, text=DETAIL_HTML, headers={"content-type": "text/html"})
    if path == "/api/v1/items/search":
        assert "json" in request.headers["accept"]
        return httpx.Response(200, json={"results": [{"id": 9, "price": 7000, "create_date": "2026-09-28"}]})
    return httpx.Response(404, text="not found", headers={"content-type": "text/html"})


def test_pure_helpers():
    blocks = probe.extract_embedded_json(HOME_HTML)
    labels = {b["label"] for b in blocks}
    assert {"__NEXT_DATA__", "window.__STATE__"} <= labels and all(b["ok"] for b in blocks)
    outline = probe.json_outline(blocks[0]["data"])
    assert "props.items[] (1 эл.)" in outline and any("props.items[0].price: int = 5000" in s for s in outline)
    assert probe.find_api_paths(BUNDLE_JS) == ["/api/v1/categories", "/api/v1/items/search?phrase="]
    links = probe.find_links(HOME_HTML, probe.BASE_URL)
    assert "https://www.seznam.cz/" not in links["all"]
    assert links["details"] == ["https://www.sbazar.cz/uzivatel/detail/123-iphone-13"]
    assert probe.scan_markers(DETAIL_HTML)["views"] == {"zobrazení": 1}


def test_probe_run(tmp_path):
    client = BazosClient(min_delay=0, max_delay=0, transport=httpx.MockTransport(handler))
    with client:
        report = probe.Probe(tmp_path, client, max_requests=20).run("iphone")

    by_name = {f["name"]: f for f in report["fetches"]}
    assert by_name["search"]["status"] == 200
    assert by_name["detail"]["url"].endswith("/prodejce/detail/456-iphone-12")  # карточка из поиска
    assert by_name["detail"]["markers"]["views"] == {"zobrazení": 1}
    assert by_name["api_search"]["status"] == 200
    assert by_name["api_categories_tree"]["skipped"] == "запрещено robots.txt"
    assert "/api/v1/categories" in report["api_paths_found"]

    saved = {p.name for p in tmp_path.iterdir()}
    assert {"summary.json", "home.html", "search.html", "detail.html", "api_search.json"} <= saved
    assert "api_search.outline.txt" in saved and "bundles.api_paths.json" in saved
    assert not any(n.startswith("bundle0.") for n in saved)  # сами бандлы не сохраняем
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["requests_made"] == report[
        "requests_made"
    ]


def test_request_limit(tmp_path):
    client = BazosClient(min_delay=0, max_delay=0, transport=httpx.MockTransport(handler))
    with client:
        report = probe.Probe(tmp_path, client, max_requests=3).run("iphone")
    assert report["requests_made"] == 3
    assert any(f.get("skipped") == "лимит запросов" for f in report["fetches"])
