"""Тесты парсинга и сценариев скрейпера без обращения к сети."""

from __future__ import annotations

import httpx
import pytest

from src import db, scraper

LIST_HTML = """
<html><body>
<div class="maincontent">
  <div class="inzeraty inzeratyflex">
    <div class="inzeratynadpis">
      <a href="/inzerat/187654321/iphone-13-128gb.php"><img src="x.jpg" class="obrazek"></a>
      <h2 class="nadpis"><a href="/inzerat/187654321/iphone-13-128gb.php">iPhone 13 128GB</a></h2>
      <span class="velikost10"> - TOP - [26.9. 2026]</span><br>
      <div class="popis">Původně za 20 000 Kč, prodám levně.</div>
    </div>
    <div class="inzeratycena"><b><span translate="no">8 500 Kč</span></b></div>
    <div class="inzeratylok">Praha<br>110 00</div>
    <div class="inzeratyview">125 x</div>
  </div>
  <div class="inzeraty inzeratyflex">
    <div class="inzeratynadpis">
      <h2 class="nadpis"><a href="https://mobil.bazos.cz/inzerat/187650000/samsung-s23.php">Samsung S23</a></h2>
      <span class="velikost10"> - [25.9. 2026]</span>
      <div class="popis">Bez škrábanců</div>
    </div>
    <div class="inzeratycena"><b>Dohodou</b></div>
    <div class="inzeratylok">Brno<br>602 00</div>
    <div class="inzeratyview">1 204 x</div>
  </div>
  <!-- Изменённые классы: парсер всё равно должен найти карточку по ссылке -->
  <section class="item-x9">
    <h3><a href="/inzerat/187600001/pixel-8.php">Google Pixel 8</a></h3>
    <p>[1.9. 2026]</p>
    <strong class="amount-price">11 500 Kč</strong>
    <em>Ostrava 70200</em>
  </section>
</div>
</body></html>
"""

DETAIL_HTML = """
<html><body><table>
<tr><td>Cena:</td><td><b>8 500 Kč</b></td></tr>
<tr><td>Lokalita:</td><td>110 00 Praha</td></tr>
<tr><td>Vidělo:</td><td>1 234 lidí</td></tr>
</table></body></html>
"""

REMOVED_HTML = "<html><body><h1>Inzerát byl vymazán</h1></body></html>"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("12 500 Kč", 12500),
        ("8 500 Kč", 8500),
        ("Dohodou", None),
        ("V textu", None),
        ("Zdarma", 0),
        ("", None),
        ("1.250 Kč", 1250),
    ],
)
def test_parse_price(text, expected):
    assert scraper.parse_price(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("Vidělo: 125 lidí", 125), ("Vidělo:1 234 lidí", 1234), ("Videlo: 7 lidi", 7), ("nic", None)],
)
def test_parse_views(text, expected):
    assert scraper.parse_views(text) == expected


def test_parse_location_and_date():
    assert scraper.parse_location("Praha 110 00") == ("Praha", "110 00")
    assert scraper.parse_location("Ostrava 70200") == ("Ostrava", "702 00")
    assert scraper.parse_post_date(" - TOP - [26.9. 2026]") == "2026-09-26"
    assert scraper.parse_post_date("[31.2. 2026]") is None


def test_parse_listing_page():
    items = {i["id"]: i for i in scraper.parse_listing_page(LIST_HTML, "mobil", "https://mobil.bazos.cz/")}
    assert set(items) == {"187654321", "187650000", "187600001"}

    iphone = items["187654321"]
    assert iphone["title"] == "iPhone 13 128GB"
    assert iphone["price_czk"] == 8500  # не 20000 из описания
    assert (iphone["location"], iphone["psc"]) == ("Praha", "110 00")
    assert iphone["posted_at"] == "2026-09-26"
    assert iphone["list_views"] == 125
    assert iphone["url"] == "https://mobil.bazos.cz/inzerat/187654321/iphone-13-128gb.php"

    samsung = items["187650000"]
    assert samsung["price_czk"] is None
    assert samsung["list_views"] == 1204

    pixel = items["187600001"]
    assert pixel["price_czk"] == 11500
    assert pixel["psc"] == "702 00"
    assert pixel["posted_at"] == "2026-09-01"


def test_parse_detail_page():
    res = scraper.parse_detail_page(DETAIL_HTML)
    assert res.status == "active"
    assert res.views == 1234
    assert res.price_czk == 8500
    assert scraper.parse_detail_page(REMOVED_HTML).status == "removed"


def _client(handler):
    return scraper.BazosClient(min_delay=0, max_delay=0, transport=httpx.MockTransport(handler))


def test_scrape_and_update_flow(tmp_path):
    db_path = tmp_path / "t.db"
    removed = {"187650000"}
    removed_now: set[str] = set()

    def phased(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/inzerat/" in path:
            if path.split("/")[2] in removed_now:
                return httpx.Response(404)
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(200, text=LIST_HTML)

    with _client(phased) as client:
        stats = scraper.scrape(["mobil"], pages=1, db_path=db_path, client=client)
    assert stats["new"] == 3 and stats["snapshots"] == 3

    removed_now.update(removed)
    with _client(phased) as client:
        upd = scraper.update_active(db_path=db_path, client=client)
    assert upd == {"checked": 3, "active": 2, "removed": 1, "errors": 0}

    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT is_active, removed_at FROM listings WHERE id='187650000'").fetchone()
        assert row["is_active"] == 0 and row["removed_at"]
        snaps = conn.execute("SELECT COUNT(*) FROM listing_snapshots").fetchone()[0]
        assert snaps == 3 + 3  # 3 при сборе + 2 новых снимка + 1 снимок снятия


def test_removed_marker_on_200(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=REMOVED_HTML)

    with _client(handler) as client:
        assert scraper.fetch_detail(client, "https://mobil.bazos.cz/inzerat/1/x.php").status == "removed"


def test_retry_on_429():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text="ok")

    with _client(handler) as client:
        client_resp = client.get("https://mobil.bazos.cz/")
    assert client_resp is not None and client_resp.status_code == 200 and calls["n"] == 2


def test_category_urls():
    assert scraper.category_page_url("pc", 0) == "https://pc.bazos.cz/"
    assert scraper.category_page_url("pc", 2) == "https://pc.bazos.cz/40/"
