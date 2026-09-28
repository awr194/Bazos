"""Тесты парсинга и сценариев скрейпера без обращения к сети."""

from __future__ import annotations

import argparse

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


def _client(handler, robots: bool = False):
    # robots=False: в большинстве тестов обработчик не отдаёт robots.txt, проверка там не нужна.
    return scraper.BazosClient(
        min_delay=0, max_delay=0, transport=httpx.MockTransport(handler), respect_robots=robots
    )


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


@pytest.mark.parametrize(
    "title,description,expected",
    [
        ("Koupím iPhone 13", "", "demand"),
        ("SHÁNÍM Škoda Fabia", "", "demand"),
        ("Hledám MacBook Air M1", "", "demand"),
        ("- koupim RTX 3080 -", "", "demand"),
        ("iPhone 13 do 8000", "Koupím iPhone v dobrém stavu", "demand"),
        ("Vykoupím vaše auto", "", "buyout"),
        ("Výkup mobilů za hotové", "", "buyout"),
        ("Kotě hledá nový domov", "", "offer"),
        ("Hledám nového majitele pro kolo", "", "offer"),
        ("iPhone 13 128GB", "Prodám, koupím i protiúčtem", "offer"),
        ("Prodám Škoda Octavia", "", "offer"),
    ],
)
def test_classify_listing(title, description, expected):
    assert scraper.classify_listing(title, description) == expected


def test_parse_total_count():
    html = "<div class='listainzerat'>Zobrazeno 1-20 inzerátů z 12 345</div>"
    assert scraper.parse_total_count(html) == 12345
    assert scraper.parse_total_count("<p>z 987 inzerátů</p>") == 987
    assert scraper.parse_total_count("<p>nic</p>") is None


DEMAND_LIST_HTML = """
<html><body>
<div class="inzeraty"><h2 class="nadpis"><a href="/inzerat/111/k.php">Koupím iPhone 13 do 8 000 Kč</a></h2>
  <div class="inzeratycena"><b>Dohodou</b></div><div class="inzeratylok">Praha 110 00</div></div>
<div class="inzeraty"><h2 class="nadpis"><a href="/inzerat/222/p.php">iPhone 13 128GB</a></h2>
  <div class="popis">Prodám, koupím i protiúčet</div>
  <div class="inzeratycena"><b>9 000 Kč</b></div><div class="inzeratylok">Brno 602 00</div></div>
<p>Zobrazeno 1-20 inzerátů z 214</p>
</body></html>
"""


def test_demand_listing_budget_and_type():
    items = {
        i["id"]: i for i in scraper.parse_listing_page(DEMAND_LIST_HTML, "mobil", "https://mobil.bazos.cz/")
    }
    assert items["111"]["listing_type"] == "demand" and items["111"]["price_czk"] == 8000
    assert items["222"]["listing_type"] == "offer" and items["222"]["price_czk"] == 9000


BAZOS_ROBOTS = "User-agent: *\nDisallow: /search.php\nDisallow: /*hledat=\nDisallow: /*humkreis\n"


def test_scrape_demand_and_count_market(tmp_path):
    db_path = tmp_path / "d.db"
    robots_fetches = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "hledat" not in request.url.params  # поиск Bazos запрещён в robots.txt
        if request.url.path == "/robots.txt":
            robots_fetches.append(request.url.host)
            return httpx.Response(200, text=BAZOS_ROBOTS)
        return httpx.Response(200, text=DEMAND_LIST_HTML)

    with _client(handler, robots=True) as client:
        stats = scraper.scrape_demand(["mobil"], pages=1, db_path=db_path, client=client)
        rows = scraper.count_market(["mobil", "pc"], db_path=db_path, client=client)
    assert stats["new"] == 2 and stats["wanted"] == 1  # спрос — из обычной ленты рубрики
    with db.get_connection(db_path) as conn:
        types = dict(conn.execute("SELECT id, listing_type FROM listings").fetchall())
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM market_counts")}
    assert types == {"111": "demand", "222": "offer"}
    assert kinds == {"offer"}
    assert [(r["category"], r["kind"], r["total"]) for r in rows] == [
        ("mobil", "offer", 214),
        ("pc", "offer", 214),
    ]
    assert robots_fetches == ["mobil.bazos.cz", "pc.bazos.cz"]


def test_query_flag_is_disabled(capsys):
    assert scraper.main(["--query", "iphone", "--categories", "mobil"]) == 2
    assert "robots.txt" in capsys.readouterr().out


HOMEPAGE_HTML = """
<html><body>
<a href="https://auto.bazos.cz/">Auto</a> <a href="https://mobil.bazos.cz/">Mobily</a>
<a href="https://novinka.bazos.cz">Novinka</a> <a href="https://www.bazos.cz/">Bazoš</a>
<a href="https://mobil.bazos.cz/inzerat/1/x.php">iPhone</a>
</body></html>
"""

RUBRIC_HTML = """
<html><body>
<div class="barvaleva">
  <a href="/apple/">Apple</a> <a href="https://mobil.bazos.cz/samsung/">Samsung</a>
  <a href="/xiaomi/">Xiaomi</a> <a href="/20/">2</a> <a href="/inzerat/5/x.php">Inzerát</a>
  <a href="/pridat-inzerat.php">Přidat</a> <a href="https://pc.bazos.cz/notebooky/">Notebooky</a>
  <a href="/search/">Hledat</a> <a href="/apple/"><img src="a.png"></a>
</div>
</body></html>
"""


def test_parse_rubrics_and_subcategories():
    assert scraper.parse_rubrics(HOMEPAGE_HTML) == {"auto": "Auto", "mobil": "Mobily", "novinka": "Novinka"}
    subs = scraper.parse_subcategories(RUBRIC_HTML, "mobil")
    assert subs == {"apple": "Apple", "samsung": "Samsung", "xiaomi": "Xiaomi"}


def test_all_rubrics_and_urls():
    from src import config

    assert len(config.CATEGORY_LABELS) == 20 and {"auto", "reality", "zvirata"} <= set(config.CATEGORY_URLS)
    assert scraper.category_page_url("mobil", 0, "apple") == "https://mobil.bazos.cz/apple/"
    assert scraper.category_page_url("mobil", 1, "apple") == "https://mobil.bazos.cz/apple/20/"
    assert scraper.category_code(" Novinka ") == "novinka"
    with pytest.raises(argparse.ArgumentTypeError):
        scraper.category_code("bad code!")


def test_discover_and_scrape_by_subcategory(tmp_path):
    db_path = tmp_path / "s.db"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if request.url.host == "www.bazos.cz":
            return httpx.Response(200, text=HOMEPAGE_HTML)
        if request.url.path == "/":
            return httpx.Response(200, text=RUBRIC_HTML)
        if request.url.path == "/apple/":
            return httpx.Response(200, text=LIST_HTML)
        return httpx.Response(200, text="<html></html>")

    with _client(handler) as client:
        res = scraper.discover(["mobil"], db_path=db_path, client=client)
        stats = scraper.scrape_by_subcategory(["mobil"], pages=1, db_path=db_path, client=client)
    assert res["new_rubrics"] == ["novinka"]
    assert set(res["subcategories"]["mobil"]) == {"apple", "samsung", "xiaomi"}
    assert stats["subcategories"] == 3 and stats["new"] == 3
    assert "https://mobil.bazos.cz/apple/" in seen
    with db.get_connection(db_path) as conn:
        subs = {r[0] for r in conn.execute("SELECT DISTINCT subcategory FROM listings")}
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM market_counts")}
    assert subs == {"apple"}
    assert kinds <= {"subcategory"}  # замер подкатегории не перетирает итог рубрики


# Заголовки из реальной выгрузки (сентябрь 2026) и ожидаемый тип.
@pytest.mark.parametrize(
    "title,expected",
    [
        ("Termokopírka ASTRA THERM 01 KOH-I-NOOR, koupím", "demand"),
        ("Fakír z Benáres a jiné povídky - M. Pašek, KOD 211 (sháním)", "demand"),
        ("Pláště-sháníme", "demand"),
        ("Odvalovací frézka FO 6 - HLEDÁME", "demand"),
        ("kúpim použité náhradní díly", "demand"),
        ("Nabídněte TENTO(PEEM) TABURET SEDATKO PODNOŽNÍK", "demand"),
        ("Retro medved - poptavam", "demand"),
        ("Dell Xps 9320 Plus Koupím", "demand"),
        ("Mam zájem o Apple iPhone", "demand"),
        ("Sbírám STARÉ PIVNÍ LAHVE, SKLENICE, PULLITRY, KORBELE", "demand"),
        ("[SHÁNÍM] grafickou kartu 4070 ti nebo 4070 ti Super", "demand"),
        ("Odkoupení nemovitostí: Garáže, pozemky, podíly, lesy, pole", "buyout"),
        ("Práce v Německu – 30 €/hod. – hledám pravou ruku", "offer"),
        ("Rhodéský Ridgeback štěňátka s PP - poslední 2 pejsci", "offer"),
        ("Štěňátka hledají nový domov", "offer"),
        ("Prodám nebo vyměním, koupím i protiúčtem", "offer"),
        ("Canon EF-S 55-250mm f/4-5.6 IS STM", "offer"),
    ],
)
def test_classify_real_titles(title, expected):
    assert scraper.classify_listing(title) == expected


def test_reclassify_and_clear_sample(tmp_path):
    path = tmp_path / "r.db"
    scraper.seed_sample(path)
    with db.get_connection(path) as conn:
        conn.execute(
            "UPDATE listings SET listing_type = 'demand' WHERE title LIKE 'Canon%' OR title LIKE 'Dyson%'"
        )
    changes = scraper.reclassify(path)
    assert changes == {"demand->offer": 1}  # Dyson V11 Absolute вернулся в «продаю»
    assert scraper.clear_sample(path) == 50
    assert db.table_counts(path)["listings"] == 0
