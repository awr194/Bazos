"""Скрейпер Bazos.cz: сбор объявлений, счётчиков просмотров и проверка «живости».

Режимы CLI:
    python -m src.scraper --categories mobil pc --pages 2          # сбор новых объявлений
    python -m src.scraper --query "iphone 13" --categories mobil   # сбор по поисковому запросу
    python -m src.scraper --update                                 # перепроверка активных
    python -m src.scraper --demand --pages 3                       # объявления «Koupím/Sháním»
    python -m src.scraper --count                                  # общий объём рынка
    python -m src.scraper --discover                               # справочник рубрик и подкатегорий
    python -m src.scraper --by-subcategory --no-details --pages 1  # обход всех подкатегорий
    python -m src.scraper --seed-sample                            # демо-данные для дашборда

Парсинг намеренно опирается на структуру страницы (ссылки вида /inzerat/<id>/ и
ближайший общий контейнер карточки) и на регулярные выражения, а не на конкретные
CSS-классы: Bazos периодически переименовывает мелкие классы вёрстки.
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser, Node

from src import config, db

log = logging.getLogger("bazos.scraper")

# --- Регулярные выражения ----------------------------------------------------
LISTING_HREF_RE = re.compile(r"/inzerat/(\d+)/")
VIEWS_DETAIL_RE = re.compile(r"Vid[ěe]lo\s*:?\s*([\d\s\u00a0]+?)\s*(?:lid|osob)", re.IGNORECASE)
VIEWS_LIST_RE = re.compile(r"(\d[\d\s\u00a0]*)\s*x\b")
PRICE_RE = re.compile(r"(\d[\d\s\u00a0.]*)\s*K[čc]", re.IGNORECASE)
DATE_RE = re.compile(r"\[\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*\]")
PSC_RE = re.compile(r"\b(\d{3})\s?(\d{2})\b")
WS_RE = re.compile(r"[\s\u00a0]+")
# «Zobrazeno 1-20 inzerátů z 12 345» — общее число объявлений в выдаче.
TOTAL_RE = re.compile(r"Zobrazeno\s*\d+\s*[-–]\s*\d+\s*inzer\w*\s*z\s*(\d[\d\s\u00a0]*)", re.IGNORECASE)
TOTAL_FALLBACK_RE = re.compile(r"\bz\s+(\d[\d\s\u00a0]*)\s*inzer", re.IGNORECASE)

# Объявления-«спрос»: покупатель сам пишет, что ищет. Проверяем начало заголовка/описания.
DEMAND_RE = re.compile(
    r"^\W*(?:koupím|koupim|koupíme|koupime|sháním|shanim|sháníme|hledám|hledam|hledáme|"
    r"poptávám|poptavam|poptávka|poptavka|chci koupit|zájem o|mám zájem)\b",
    re.IGNORECASE,
)
# Перекупщики («Vykoupím vaše auto», «Výkup mobilů») — отдельный тип, чтобы не искажать спрос.
BUYOUT_RE = re.compile(r"^\W*(?:vykoupím|vykoupim|vykoupíme|vykoupime|výkup|vykup)\b", re.IGNORECASE)
# «Hledám nového majitele» — это продажа, а не поиск.
FALSE_DEMAND_RE = re.compile(r"nov(?:ého|eho|ý|y)\s+(?:majitele|páníčka|domov)", re.IGNORECASE)


@dataclass
class DetailResult:
    """Результат запроса страницы объявления."""

    status: str  # "active" | "removed" | "error"
    views: int | None = None
    price_czk: int | None = None


# --- Утилиты парсинга (чистые функции, легко тестируются) --------------------
def clean_text(text: str | None) -> str:
    return WS_RE.sub(" ", text or "").strip()


def parse_int(text: str | None) -> int | None:
    """Извлекает целое из строки с разделителями разрядов: '12 500' -> 12500."""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def parse_price(text: str | None) -> int | None:
    """'12 500 Kč' -> 12500; 'Dohodou'/'V textu' -> None; 'Zdarma' -> 0."""
    t = clean_text(text).lower()
    if not t:
        return None
    if "zdarma" in t:
        return 0
    m = PRICE_RE.search(t)
    if m:
        return parse_int(m.group(1))
    # Иногда «Kč» вынесено в отдельный элемент — берём число, если строка состоит из цифр.
    if re.fullmatch(r"[\d\s.]+", t):
        return parse_int(t)
    return None


def parse_views(text: str | None) -> int | None:
    """'Vidělo: 1 234 lidí' -> 1234."""
    m = VIEWS_DETAIL_RE.search(clean_text(text))
    return parse_int(m.group(1)) if m else None


def parse_post_date(text: str | None) -> str | None:
    """'TOP - [26.9. 2026]' -> '2026-09-26'."""
    m = DATE_RE.search(text or "")
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_location(text: str | None) -> tuple[str | None, str | None]:
    """'Praha 110 00' -> ('Praha', '110 00')."""
    t = clean_text(text)
    if not t:
        return None, None
    matches = list(PSC_RE.finditer(t))
    m = matches[-1] if matches else None  # берём последнее совпадение — PSČ стоит в конце
    if m is None:
        return t or None, None
    psc = f"{m.group(1)} {m.group(2)}"
    city = clean_text(t[: m.start()] + t[m.end() :]) or None
    return city, psc


def classify_listing(title: str | None, description: str | None = None) -> str:
    """offer — продаю, demand — «Koupím/Sháním/Hledám», buyout — перекупщики («Vykoupím»)."""
    t = clean_text(title)
    if BUYOUT_RE.search(t):
        return "buyout"
    if DEMAND_RE.search(t) and not FALSE_DEMAND_RE.search(t):
        return "demand"
    # Заголовок без глагола («iPhone 13 do 8000») — смотрим начало описания.
    d = clean_text(description)[:80]
    if d and DEMAND_RE.search(d) and not FALSE_DEMAND_RE.search(d):
        return "demand"
    return "offer"


RUBRIC_HREF_RE = re.compile(r"^https?://([a-z0-9-]+)\.bazos\.cz/?$", re.IGNORECASE)
SUBCATEGORY_PATH_RE = re.compile(r"^/([a-z0-9][a-z0-9-]*)/?$")
# Служебные разделы, которые выглядят как подкатегории, но ими не являются.
NOT_SUBCATEGORIES = frozenset({"inzerat", "search", "hledat", "pridat", "moje", "kontakt", "napoveda"})


def parse_rubrics(html: str) -> dict[str, str]:
    """Рубрики с главной www.bazos.cz: {код поддомена: название}."""
    rubrics: dict[str, str] = {}
    for a in HTMLParser(html).css("a[href]"):
        m = RUBRIC_HREF_RE.match((a.attributes.get("href") or "").strip())
        name = _node_text(a)
        if m and m.group(1).lower() != "www" and name:
            rubrics.setdefault(m.group(1).lower(), name)
    return rubrics


def parse_subcategories(html: str, category: str) -> dict[str, str]:
    """Подкатегории со страницы рубрики: {slug: название}, например {"apple": "Apple"}.

    Ищем ссылки на пути из одного сегмента (/apple/ или https://mobil.bazos.cz/apple/),
    исключая пагинацию (/20/), объявления и служебные страницы (*.php).
    """
    host = f"{category}.bazos.cz"
    subs: dict[str, str] = {}
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc.lower() != host:
            continue
        m = SUBCATEGORY_PATH_RE.match(parsed.path or "")
        name = _node_text(a)
        if not m or not name or parsed.query:
            continue
        slug = m.group(1).lower()
        if slug.isdigit() or slug in NOT_SUBCATEGORIES:
            continue
        subs.setdefault(slug, name)
    return subs


def parse_total_count(html: str) -> int | None:
    """Общее число объявлений в рубрике/выдаче поиска («… inzerátů z 12 345»)."""
    text = clean_text(HTMLParser(html).text(separator=" "))
    m = TOTAL_RE.search(text) or TOTAL_FALLBACK_RE.search(text)
    return parse_int(m.group(1)) if m else None


def _node_text(node: Node | None) -> str:
    return clean_text(node.text(separator=" ")) if node is not None else ""


def _find_by_class_fragment(card: Node, fragments: Iterable[str]) -> Node | None:
    """Ищет потомка, у которого класс содержит один из фрагментов (устойчиво к переименованиям)."""
    for frag in fragments:
        node = card.css_first(f'[class*="{frag}"]')
        if node is not None:
            return node
    return None


def _card_for_anchor(anchor: Node, listing_id: str) -> Node:
    """Поднимается от ссылки вверх, пока контейнер содержит ссылки только на это объявление."""
    card = anchor
    parent = anchor.parent
    while parent is not None and parent.tag not in ("body", "html"):
        ids = {
            m.group(1)
            for a in parent.css("a[href]")
            if (m := LISTING_HREF_RE.search(a.attributes.get("href") or ""))
        }
        if ids != {listing_id}:
            break
        card = parent
        parent = parent.parent
    return card


def parse_listing_page(
    html: str, category: str, base_url: str, query: str | None = None
) -> list[dict[str, Any]]:
    """Разбирает страницу категории/поиска в список словарей-объявлений."""
    tree = HTMLParser(html)
    anchors: dict[str, list[Node]] = {}
    for a in tree.css("a[href]"):
        m = LISTING_HREF_RE.search(a.attributes.get("href") or "")
        if m:
            anchors.setdefault(m.group(1), []).append(a)

    results: list[dict[str, Any]] = []
    for listing_id, links in anchors.items():
        # Заголовок — ссылка с самым длинным текстом (картинка-ссылка текста не имеет).
        title_a = max(links, key=lambda a: len(_node_text(a)))
        title = _node_text(title_a)
        if not title:
            continue
        card = _card_for_anchor(title_a, listing_id)
        card_text = _node_text(card)

        # Описание исключаем из поиска цены/просмотров, чтобы не ловить «původně 20 000 Kč».
        popis = _find_by_class_fragment(card, ("popis",))
        scan_text = card_text.replace(_node_text(popis), " ") if popis is not None else card_text

        price_node = _find_by_class_fragment(card, ("cena", "price"))
        price = parse_price(_node_text(price_node)) if price_node is not None else None
        if price is None:
            price = parse_price(scan_text)

        loc_node = _find_by_class_fragment(card, ("lok", "lokalita", "location"))
        loc_text = _node_text(loc_node) if loc_node is not None else ""
        location, psc = parse_location(loc_text)
        if psc is None:
            _, psc = parse_location(scan_text)

        listing_type = classify_listing(title, _node_text(popis))
        if price is None and listing_type == "demand":
            price = parse_price(title)  # бюджет покупателя: «Koupím iPhone 13 do 8 000 Kč»

        views = None
        view_node = _find_by_class_fragment(card, ("view",))
        if view_node is not None:
            vm = VIEWS_LIST_RE.search(_node_text(view_node))
            views = parse_int(vm.group(1)) if vm else None

        results.append(
            {
                "id": listing_id,
                "category": category,
                "query": query,
                "title": title,
                "listing_type": listing_type,
                "price_czk": price,
                "location": location,
                "psc": psc,
                "url": urljoin(base_url, title_a.attributes.get("href") or ""),
                "posted_at": parse_post_date(card_text),
                "list_views": views,
            }
        )
    return results


def is_removed_page(html: str) -> bool:
    low = clean_text(html).lower()
    return any(marker in low for marker in config.REMOVED_MARKERS)


def parse_detail_page(html: str) -> DetailResult:
    """Извлекает просмотры и цену со страницы объявления или определяет, что оно удалено."""
    if is_removed_page(html):
        return DetailResult(status="removed")
    tree = HTMLParser(html)
    body = tree.body
    text = _node_text(body) if body is not None else clean_text(html)
    views = parse_views(text)
    price = None
    # Строка таблицы вида «Cena: 12 500 Kč» — ищем по тексту, а не по классам.
    m = re.search(r"Cena\s*:\s*([^|]{0,40}?K[čc]|Dohodou|V textu|Zdarma)", text, re.IGNORECASE)
    if m:
        price = parse_price(m.group(1))
    return DetailResult(status="active", views=views, price_czk=price)


# --- HTTP-клиент с ограничением скорости -------------------------------------
class BazosClient:
    """Обёртка над httpx.Client: случайный User-Agent, джиттер 1.5–3.5 с, повторы при 429/5xx."""

    def __init__(
        self,
        min_delay: float = config.MIN_DELAY,
        max_delay: float = config.MAX_DELAY,
        max_retries: int = config.MAX_RETRIES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self._last_request = 0.0
        self._client = httpx.Client(
            headers=config.BASE_HEADERS,
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
            transport=transport,  # позволяет подменить сеть в тестах (httpx.MockTransport)
        )

    def __enter__(self) -> BazosClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        delay = random.uniform(self.min_delay, self.max_delay)
        wait = self._last_request + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response | None:
        """GET с джиттером и повторами. Возвращает ответ (в т.ч. 404) или None при сбое сети."""
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            headers = {"User-Agent": random.choice(config.USER_AGENTS)}
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("Сетевая ошибка %s (попытка %d): %s", url, attempt, exc)
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = parse_int(resp.headers.get("Retry-After"))
                    pause = (
                        retry_after if retry_after is not None else config.RETRY_BACKOFF * 2 ** (attempt - 1)
                    )
                    log.warning("HTTP %s для %s — пауза %.0f с", resp.status_code, url, pause)
                    time.sleep(pause)
                    continue
                return resp
        return None


def category_page_url(category: str, page: int, subcategory: str | None = None) -> str:
    base = config.category_url(category)
    if subcategory:
        base = urljoin(base, f"{subcategory}/")
    offset = page * config.LISTINGS_PER_PAGE
    return base if offset == 0 else urljoin(base, f"{offset}/")


def search_params(query: str) -> dict[str, str]:
    return {
        "hledat": query,
        "hlokalita": "",
        "humkreis": "25",
        "cenaod": "",
        "cenado": "",
        "Submit": "Hledat",
        "kitx": "ano",
    }


def fetch_detail(client: BazosClient, url: str) -> DetailResult:
    resp = client.get(url)
    if resp is None:
        return DetailResult(status="error")
    if resp.status_code in (404, 410):
        return DetailResult(status="removed")
    if resp.status_code != 200:
        return DetailResult(status="error")
    # Bazos может перенаправить удалённое объявление на главную рубрики.
    if not LISTING_HREF_RE.search(str(resp.url)):
        return DetailResult(status="removed")
    return parse_detail_page(resp.text)


def market_kind(query: str | None) -> str:
    if not query:
        return "offer"
    return "demand" if query in config.DEMAND_QUERIES else "search"


# --- Сценарии ------------------------------------------------------------------
def scrape(
    categories: Iterable[str],
    pages: int = config.DEFAULT_PAGES,
    query: str | None = None,
    fetch_details: bool = True,
    detail_limit: int | None = None,
    db_path: Path | str | None = None,
    client: BazosClient | None = None,
    subcategory: str | None = None,
) -> dict[str, int]:
    """Собирает объявления с первых `pages` страниц каждой категории и делает снимки просмотров.

    `subcategory` — обходить не всю рубрику, а её подкатегорию (объявления получат этот тег).
    """
    db.init_db(db_path)
    stats = {"pages": 0, "found": 0, "new": 0, "snapshots": 0}
    own_client = client is None
    client = client or BazosClient()
    try:
        for category in categories:
            details_done = 0
            for page in range(pages):
                url = category_page_url(category, page, subcategory)
                resp = client.get(url, params=search_params(query) if query else None)
                if resp is None or resp.status_code != 200:
                    log.error("Не удалось загрузить %s", url)
                    break
                items = parse_listing_page(resp.text, category, str(resp.url), query)
                for item in items:
                    item["subcategory"] = subcategory
                if page == 0:
                    total = parse_total_count(resp.text)
                    if total is not None:
                        # Для подкатегории сохраняем отдельный вид замера, чтобы не перетереть итог рубрики.
                        kind, q = ("subcategory", subcategory) if subcategory else (market_kind(query), query)
                        if subcategory and query:
                            kind, q = "search", f"{subcategory}:{query}"
                        with db.get_connection(db_path) as conn:
                            db.add_market_count(conn, db.utcnow(), category, kind, total, q)
                stats["pages"] += 1
                stats["found"] += len(items)
                log.info(
                    "%s%s стр.%d: %d объявлений",
                    category,
                    f"/{subcategory}" if subcategory else "",
                    page + 1,
                    len(items),
                )
                if not items:
                    break
                for item in items:
                    views = item.pop("list_views")
                    price = item.get("price_czk")
                    if fetch_details and (detail_limit is None or details_done < detail_limit):
                        detail = fetch_detail(client, item["url"])
                        details_done += 1
                        if detail.status == "active":
                            views = detail.views if detail.views is not None else views
                            price = detail.price_czk if detail.price_czk is not None else price
                    now = db.utcnow()
                    with db.get_connection(db_path) as conn:  # короткая транзакция на объявление
                        stats["new"] += db.upsert_listing(conn, item, now)
                        if views is not None:
                            db.add_snapshot(conn, item["id"], now, views, price)
                            stats["snapshots"] += 1
    finally:
        if own_client:
            client.close()
    return stats


def discover(
    categories: Iterable[str] | None = None,
    db_path: Path | str | None = None,
    client: BazosClient | None = None,
) -> dict[str, Any]:
    """Обновляет справочник: рубрики с главной www.bazos.cz и подкатегории каждой рубрики.

    Если `categories` не заданы — берутся все рубрики, найденные на главной, плюс известные из config.
    """
    db.init_db(db_path)
    own_client = client is None
    client = client or BazosClient()
    result: dict[str, Any] = {"rubrics": {}, "new_rubrics": [], "subcategories": {}}
    try:
        resp = client.get(config.HOMEPAGE_URL)
        rubrics = parse_rubrics(resp.text) if resp is not None and resp.status_code == 200 else {}
        result["rubrics"] = rubrics
        result["new_rubrics"] = sorted(set(rubrics) - set(config.CATEGORY_LABELS))
        targets = list(categories) if categories else sorted(set(rubrics) | set(config.CATEGORY_LABELS))
        now = db.utcnow()
        with db.get_connection(db_path) as conn:
            for code in targets:
                db.upsert_taxonomy(conn, code, "", rubrics.get(code), config.category_url(code), now)
        for code in targets:
            resp = client.get(config.category_url(code))
            if resp is None or resp.status_code != 200:
                log.warning("Не удалось открыть рубрику %s", code)
                continue
            subs = parse_subcategories(resp.text, code)
            result["subcategories"][code] = subs
            with db.get_connection(db_path) as conn:
                for slug, name in subs.items():
                    url = urljoin(config.category_url(code), f"{slug}/")
                    db.upsert_taxonomy(conn, code, slug, name, url, now)
            log.info("%s: %d подкатегорий", code, len(subs))
    finally:
        if own_client:
            client.close()
    return result


def scrape_by_subcategory(
    categories: Iterable[str],
    pages: int = 1,
    query: str | None = None,
    fetch_details: bool = False,
    detail_limit: int | None = None,
    db_path: Path | str | None = None,
    client: BazosClient | None = None,
) -> dict[str, int]:
    """Обходит каждую известную подкатегорию (сначала запустите --discover) — объявления получают тег."""
    rows = [r for r in db.list_taxonomy(db_path, categories) if r["subcategory"]]
    total = {"subcategories": len(rows), "pages": 0, "found": 0, "new": 0, "snapshots": 0}
    if not rows:
        log.warning("Подкатегории не найдены — сначала выполните: python -m src.scraper --discover")
        return total
    own_client = client is None
    client = client or BazosClient()
    try:
        for r in rows:
            stats = scrape(
                [r["category"]], pages, query, fetch_details, detail_limit, db_path, client, r["subcategory"]
            )
            for k in ("pages", "found", "new", "snapshots"):
                total[k] += stats[k]
    finally:
        if own_client:
            client.close()
    return total


def scrape_demand(
    categories: Iterable[str],
    pages: int = 1,
    queries: Iterable[str] = config.DEMAND_QUERIES,
    fetch_details: bool = False,
    detail_limit: int | None = None,
    db_path: Path | str | None = None,
    client: BazosClient | None = None,
) -> dict[str, int]:
    """Собирает объявления «Koupím / Sháním / Hledám» — то, что люди сами ищут."""
    total = {"pages": 0, "found": 0, "new": 0, "snapshots": 0}
    own_client = client is None
    client = client or BazosClient()
    try:
        for q in queries:
            stats = scrape(categories, pages, q, fetch_details, detail_limit, db_path, client)
            for k in total:
                total[k] += stats[k]
    finally:
        if own_client:
            client.close()
    return total


def count_market(
    categories: Iterable[str],
    queries: Iterable[str] = config.DEMAND_QUERIES,
    db_path: Path | str | None = None,
    client: BazosClient | None = None,
) -> list[dict[str, Any]]:
    """Только считает объём рынка: сколько всего объявлений в рубрике и по запросам спроса.

    1 запрос на рубрику + 1 на каждый запрос спроса; сами объявления не сохраняются.
    """
    db.init_db(db_path)
    rows: list[dict[str, Any]] = []
    own_client = client is None
    client = client or BazosClient()
    try:
        for category in categories:
            for q in [None, *queries]:
                resp = client.get(config.category_url(category), params=search_params(q) if q else None)
                ok = resp is not None and resp.status_code == 200
                total = parse_total_count(resp.text) if ok else None
                rows.append({"category": category, "kind": market_kind(q), "query": q, "total": total})
                if total is not None:
                    with db.get_connection(db_path) as conn:
                        db.add_market_count(conn, db.utcnow(), category, market_kind(q), total, q)
                else:
                    log.warning("Не удалось определить общее число для %s / %s", category, q or "—")
    finally:
        if own_client:
            client.close()
    return rows


def update_active(
    categories: Iterable[str] | None = None,
    limit: int | None = None,
    db_path: Path | str | None = None,
    client: BazosClient | None = None,
) -> dict[str, int]:
    """Перепроверяет активные объявления: новый снимок просмотров или is_active = 0."""
    db.init_db(db_path)
    rows = db.active_listings(db_path, categories)
    if limit:
        rows = rows[:limit]
    stats = {"checked": 0, "active": 0, "removed": 0, "errors": 0}
    own_client = client is None
    client = client or BazosClient()
    try:
        for row in rows:
            result = fetch_detail(client, row["url"])
            now = db.utcnow()
            stats["checked"] += 1
            with db.get_connection(db_path) as conn:
                if result.status == "removed":
                    db.mark_inactive(conn, row["id"], now)
                    stats["removed"] += 1
                    log.info("Снято: %s", row["url"])
                elif result.status == "active":
                    db.add_snapshot(conn, row["id"], now, result.views, result.price_czk or row["price_czk"])
                    conn.execute(
                        "UPDATE listings SET last_seen = ?, last_checked = ? WHERE id = ?",
                        (now, now, row["id"]),
                    )
                    stats["active"] += 1
                else:
                    conn.execute("UPDATE listings SET last_checked = ? WHERE id = ?", (now, row["id"]))
                    stats["errors"] += 1
    finally:
        if own_client:
            client.close()
    return stats


# --- Демо-данные ---------------------------------------------------------------
SAMPLE_ITEMS: dict[str, list[tuple[str, int, int, str]]] = {
    # (заголовок, цена CZK, «горячесть» 1..10 — влияет на VPH и скорость продажи, подкатегория)
    "mobil": [
        ("iPhone 13 128GB, baterie 89 %", 8900, 9, "apple"),
        ("iPhone 14 Pro 256GB fialový", 17500, 8, "apple"),
        ("Samsung Galaxy S23 Ultra 512GB", 16900, 7, "samsung"),
        ("Xiaomi Redmi Note 12 Pro", 3900, 5, "xiaomi"),
        ("Google Pixel 8 128GB záruka", 11500, 6, "google"),
        ("iPhone 11 64GB černý", 4990, 9, "apple"),
        ("Samsung Galaxy A54 5G", 4500, 4, "samsung"),
        ("iPhone 15 128GB nový, nerozbalený", 18900, 10, "apple"),
        ("OnePlus 11 16/256GB", 9900, 5, "oneplus"),
    ],
    "pc": [
        ("Herní PC RTX 3070, Ryzen 5 5600X", 17900, 8, "pocitace"),
        ("MacBook Air M1 8/256GB", 13500, 9, "notebooky"),
        ("Lenovo ThinkPad T480 i5 16GB", 5900, 7, "notebooky"),
        ("Grafická karta RTX 4060 Ti", 9800, 8, "komponenty"),
        ('Monitor Dell 27" 144Hz', 3900, 5, "monitory"),
        ("MacBook Pro 14 M2 Pro", 38900, 6, "notebooky"),
        ("SSD Samsung 990 Pro 2TB", 3200, 6, "komponenty"),
        ("PS5 Digital Edition + 2 ovladače", 8500, 10, "konzole"),
    ],
    "elektro": [
        ("Robotický vysavač Roborock S7", 6500, 7, "vysavace"),
        ("Dyson V11 Absolute", 7900, 8, "vysavace"),
        ('Televize LG OLED 55" C1', 16900, 6, "televize"),
        ("Kávovar DeLonghi Magnifica S", 4200, 7, "kuchyne"),
        ("AirPods Pro 2. generace", 3900, 9, "audio"),
        ("Sony WH-1000XM4 sluchátka", 3500, 8, "audio"),
        ("Pračka Bosch Serie 6, 8 kg", 6900, 5, "pracky"),
        ("Apple Watch Series 8 45mm", 6200, 7, "hodinky"),
    ],
    "auto": [
        ("Škoda Octavia III 2.0 TDI 2017", 239000, 8, "skoda"),
        ("VW Golf VII 1.4 TSI 2016", 219000, 7, "volkswagen"),
        ("Škoda Fabia II 1.2 HTP 2010", 69000, 9, "skoda"),
        ("Ford Focus 1.6 TDCi kombi 2012", 89000, 5, "ford"),
        ("Toyota Yaris 1.33 2011", 99000, 7, "toyota"),
        ("Škoda Superb II 2.0 TDI DSG", 289000, 5, "skoda"),
        ("Dacia Duster 1.6 4x4 2015", 175000, 6, "dacia"),
        ("Zimní pneu 205/55 R16 sada", 3500, 8, "pneumatiky"),
    ],
    "motorky": [("Yamaha MT-07 2019, 18 000 km", 159000, 7, "yamaha")],
    "sport": [("Horské kolo Specialized Rockhopper 29", 14500, 8, "cyklistika")],
    "nabytek": [("Rohová sedačka rozkládací", 6500, 5, "sedaci-soupravy")],
    "dum": [("Aku vrtačka Makita 18V + 2 baterie", 3900, 8, "naradi")],
    "deti": [("Kočárek Cybex Priam 3v1", 12900, 7, "kocarky")],
    "hudba": [("Elektrická kytara Fender Stratocaster", 16500, 5, "kytary")],
    "foto": [("Sony A7 III tělo, 25 000 cvaků", 32000, 6, "fotoaparaty")],
    "zvirata": [("Akvárium 240 l s příslušenstvím", 4500, 3, "akvaristika")],
}
SAMPLE_LOCATIONS = [
    ("Praha", "110 00"),
    ("Brno", "602 00"),
    ("Ostrava", "702 00"),
    ("Plzeň", "301 00"),
    ("Olomouc", "779 00"),
    ("Liberec", "460 01"),
    ("České Budějovice", "370 01"),
    ("Hradec Králové", "500 02"),
    ("Pardubice", "530 02"),
    ("Zlín", "760 01"),
]
SAMPLE_QUERIES = {
    "iphone": "iphone",
    "macbook": "macbook",
    "škoda": "škoda",
    "rtx": "rtx",
    "samsung": "samsung",
}
# Объявления покупателей: (категория, заголовок, бюджет CZK или None, «горячесть», тип, подкатегория).
SAMPLE_WANTED: list[tuple[str, str, int | None, int, str, str | None]] = [
    ("mobil", "Koupím iPhone 13 do 8 000 Kč", 8000, 7, "demand", "apple"),
    ("mobil", "Koupím iPhone 14 Pro, i s vadou", None, 6, "demand", "apple"),
    ("mobil", "Sháním Samsung Galaxy S23", 12000, 4, "demand", "samsung"),
    ("pc", "Sháním MacBook Air M1, rozumná cena", 11000, 6, "demand", "notebooky"),
    ("pc", "Koupím RTX 3070 / RTX 3080", 7000, 5, "demand", "komponenty"),
    ("elektro", "Hledám Dyson V11 do 6 000 Kč", 6000, 4, "demand", "vysavace"),
    ("auto", "Koupím Škoda Octavia III, do 200 000 Kč", 200000, 6, "demand", "skoda"),
    ("auto", "Sháním Škoda Fabia II pro dceru", 60000, 5, "demand", "skoda"),
    ("auto", "Vykoupím vaše auto – platba ihned", None, 3, "buyout", None),
]
SAMPLE_ID_BASE = 900_000_000  # диапазон, не пересекающийся с реальными ID


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def seed_sample(db_path: Path | str | None = None, seed: int = 42) -> int:
    """Создаёт 50 правдоподобных объявлений (41 продажа + 9 «куплю») со снимками просмотров."""
    rng = random.Random(seed)
    db.init_db(db_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    count = 0
    with db.get_connection(db_path) as conn:
        conn.execute("DELETE FROM listings WHERE CAST(id AS INTEGER) >= ?", (SAMPLE_ID_BASE,))
        records = [(c, t, p, h, "offer", sc) for c, items in SAMPLE_ITEMS.items() for t, p, h, sc in items]
        for category, title, price, heat, listing_type, subcategory in records + SAMPLE_WANTED:
            count += 1
            listing_id = str(SAMPLE_ID_BASE + count)
            city, psc = rng.choice(SAMPLE_LOCATIONS)
            first_seen = now - timedelta(hours=rng.uniform(6, 120))
            posted = first_seen - timedelta(hours=rng.uniform(0, 30))
            query = next((q for k, q in SAMPLE_QUERIES.items() if k in title.lower()), None)
            vph = max(0.3, rng.gauss(heat * 2.2, heat * 0.5))
            views0 = rng.randint(5, 40 + heat * 15)
            # Горячие объявления уходят быстрее: время жизни ~ 4–120 ч.
            lifetime_h = rng.uniform(4, 22) + (10 - heat) * rng.uniform(3, 11)
            removed = first_seen + timedelta(hours=lifetime_h)
            is_active = removed > now
            end = now if is_active else removed
            item = {
                "id": listing_id,
                "category": category,
                "subcategory": subcategory,
                "query": query,
                "title": title,
                "listing_type": listing_type,
                "price_czk": price,
                "location": city,
                "psc": psc,
                "url": f"{config.category_url(category)}inzerat/{listing_id}/{_slug(title)}.php",
                "posted_at": posted.date().isoformat(),
            }
            db.upsert_listing(conn, item, db.fmt_ts(first_seen))
            # Снимки каждые ~3–8 часов между first_seen и end.
            t = last_snap = first_seen
            views = views0
            while t <= end:
                db.add_snapshot(conn, listing_id, db.fmt_ts(t), int(views), price)
                last_snap = t
                step = rng.uniform(3, 8)
                t += timedelta(hours=step)
                views += vph * step * rng.uniform(0.7, 1.3)
            conn.execute(
                "UPDATE listings SET last_seen = ?, last_checked = ? WHERE id = ?",
                (db.fmt_ts(last_snap), db.fmt_ts(last_snap), listing_id),
            )
            if not is_active:
                db.mark_inactive(conn, listing_id, db.fmt_ts(removed))
    return count


# --- CLI -------------------------------------------------------------------------
def category_code(value: str) -> str:
    """Проверка кода рубрики для argparse: любой поддомен Bazos, не только из списка."""
    code = value.strip().lower()
    if not re.match(config.CATEGORY_CODE_RE, code):
        raise argparse.ArgumentTypeError(f"некорректный код рубрики: {value!r}")
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.scraper",
        description="Сбор объявлений Bazos.cz и отслеживание спроса.",
    )
    p.add_argument(
        "--categories",
        nargs="+",
        type=category_code,
        default=None,
        metavar="CODE",
        help=f"рубрики (поддомены Bazos); по умолчанию все {len(config.CATEGORY_LABELS)}: "
        + ", ".join(config.CATEGORY_LABELS),
    )
    p.add_argument(
        "--subcategory", help="обходить только эту подкатегорию (slug, напр. apple) выбранной рубрики"
    )
    p.add_argument(
        "--discover",
        action="store_true",
        help="обновить справочник рубрик и подкатегорий с сайта и выйти",
    )
    p.add_argument(
        "--by-subcategory",
        action="store_true",
        help="обходить каждую подкатегорию из справочника (объявления получают тег подкатегории)",
    )
    p.add_argument(
        "--pages",
        type=int,
        default=config.DEFAULT_PAGES,
        help="сколько страниц (по 20 объявлений) обходить в каждой категории",
    )
    p.add_argument("--query", help="поисковый запрос (hledat) внутри категорий")
    p.add_argument(
        "--no-details",
        action="store_true",
        help="не открывать страницы объявлений (просмотры только из списка)",
    )
    p.add_argument("--detail-limit", type=int, default=None, help="максимум страниц объявлений на категорию")
    p.add_argument(
        "--update", action="store_true", help="перепроверить активные объявления (снимок просмотров / снятие)"
    )
    p.add_argument("--limit", type=int, default=None, help="лимит объявлений для --update")
    p.add_argument(
        "--demand",
        action="store_true",
        help="собрать объявления покупателей («koupím», «sháním», «hledám») — что люди ищут",
    )
    p.add_argument(
        "--count",
        action="store_true",
        help="только посчитать общий объём рынка по рубрикам и запросам спроса (без сохранения объявлений)",
    )
    p.add_argument(
        "--seed-sample", action="store_true", help="заполнить базу демо-данными (50 объявлений) и выйти"
    )
    p.add_argument("--init-db", action="store_true", help="только создать таблицы и выйти")
    p.add_argument("--db", type=Path, default=None, help=f"путь к SQLite (по умолчанию {config.DB_PATH})")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.init_db:
        path = db.init_db(args.db)
        print(f"База готова: {path} {db.table_counts(path)}")
        return 0
    if args.seed_sample:
        n = seed_sample(args.db)
        print(f"Добавлено демо-объявлений: {n}. Состояние базы: {db.table_counts(args.db)}")
        return 0
    if args.discover:
        res = discover(args.categories, args.db)
        for code, subs in res["subcategories"].items():
            label = config.CATEGORY_LABELS.get(code, res["rubrics"].get(code, code))
            print(f"{code:<10} {label:<22} подкатегорий: {len(subs):>3}  {', '.join(list(subs)[:8])}")
        if res["new_rubrics"]:
            print(f"Новые рубрики на сайте (нет в config): {', '.join(res['new_rubrics'])}")
        return 0
    if args.update:
        stats = update_active(args.categories, args.limit, args.db)
        print(f"Проверка завершена: {stats}")
        return 0
    categories = args.categories or list(config.CATEGORY_LABELS)
    if args.subcategory and len(categories) != 1:
        print("--subcategory требует ровно одну рубрику: --categories mobil --subcategory apple")
        return 2
    if args.by_subcategory:
        stats = scrape_by_subcategory(
            categories,
            args.pages,
            args.query,
            fetch_details=not args.no_details,
            detail_limit=args.detail_limit,
            db_path=args.db,
        )
        print(f"Сбор по подкатегориям завершён: {stats}")
        return 0
    if args.count:
        for row in count_market(categories, db_path=args.db):
            total = "н/д" if row["total"] is None else f"{row['total']:,}".replace(",", " ")
            print(f"{row['category']:<8} {row['kind']:<7} {row['query'] or '(вся рубрика)':<14} {total}")
        return 0
    if args.demand:
        stats = scrape_demand(
            categories,
            args.pages,
            fetch_details=not args.no_details,
            detail_limit=args.detail_limit,
            db_path=args.db,
        )
        print(f"Сбор спроса завершён: {stats}")
        return 0
    stats = scrape(
        categories,
        args.pages,
        args.query,
        fetch_details=not args.no_details,
        detail_limit=args.detail_limit,
        db_path=args.db,
        subcategory=args.subcategory,
    )
    print(f"Сбор завершён: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
