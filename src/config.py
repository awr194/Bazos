"""Конфигурация: константы, URL категорий, User-Agent'ы и параметры ограничения скорости."""

from __future__ import annotations

import os
from pathlib import Path

# --- Пути -------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.environ.get("BAZOS_DB_PATH", DATA_DIR / "bazos.db"))

# --- Категории Bazos.cz ------------------------------------------------------
# Все рубрики сайта. Каждая живёт на своём поддомене https://<код>.bazos.cz/,
# подкатегории — на путях вида https://mobil.bazos.cz/apple/ (обнаруживаются через --discover).
# Пагинация — смещение по 20: /20/, /40/, ...
CATEGORY_LABELS: dict[str, str] = {
    "zvirata": "Животные",
    "deti": "Детские товары",
    "reality": "Недвижимость",
    "prace": "Работа",
    "auto": "Автомобили",
    "motorky": "Мотоциклы",
    "stroje": "Техника и станки",
    "dum": "Дом и сад",
    "pc": "Компьютеры",
    "mobil": "Мобильные телефоны",
    "foto": "Фото",
    "elektro": "Электроника",
    "sport": "Спорт",
    "hudba": "Музыка",
    "vstupenky": "Билеты",
    "knihy": "Книги",
    "nabytek": "Мебель",
    "obleceni": "Одежда",
    "sluzby": "Услуги",
    "ostatni": "Прочее",
}
CATEGORY_CODE_RE = r"^[a-z0-9-]{2,30}$"


def category_url(code: str) -> str:
    """URL рубрики по её коду (поддомену), в т.ч. для рубрик, которых нет в списке выше."""
    return f"https://{code}.bazos.cz/"


CATEGORY_URLS: dict[str, str] = {code: category_url(code) for code in CATEGORY_LABELS}
HOMEPAGE_URL = "https://www.bazos.cz/"
LISTINGS_PER_PAGE = 20
DEFAULT_PAGES = 2

# --- HTTP --------------------------------------------------------------------
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]
BASE_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.6",
    "Cache-Control": "no-cache",
}
REQUEST_TIMEOUT = 20.0  # секунды
MAX_RETRIES = 3
RETRY_BACKOFF = 5.0  # базовая пауза (сек) при 429/5xx, растёт экспоненциально

# --- Ограничение скорости ----------------------------------------------------
# Случайная пауза перед каждым запросом, чтобы не нагружать сайт.
MIN_DELAY = 1.5
MAX_DELAY = 3.5

# --- Признаки удалённого объявления -----------------------------------------
REMOVED_MARKERS: tuple[str, ...] = (
    "inzerát byl vymazán",
    "inzerát byl smazán",
    "inzerát neexistuje",
    "inzerát nebyl nalezen",
)

# --- Спрос --------------------------------------------------------------------
# Слова спроса («Koupím iPhone 13»). Поиск Bazos (?hledat=) запрещён в robots.txt, поэтому
# по ним больше не ищем: объявления покупателей берутся из обычной ленты рубрик (--demand).
DEMAND_QUERIES: tuple[str, ...] = ("koupím", "sháním", "hledám")
DEMAND_PAGES = 3  # сколько страниц каждой рубрики обходить в режиме --demand

# --- Демо-данные -------------------------------------------------------------
SAMPLE_ID_BASE = 900_000_000  # ID демо-объявлений (--seed-sample); реальные ID Bazos меньше

# --- Цены-заглушки -----------------------------------------------------------
# Продавцы и покупатели часто ставят «1 Kč», «123456» или максимум поля (2 147 483 647),
# чтобы не указывать цену. Такие значения считаем отсутствующей ценой.
MIN_REAL_PRICE = 2
MAX_REAL_PRICE = 50_000_000
PLACEHOLDER_PRICES: frozenset[int] = frozenset(
    {12345, 123456, 1234567, 12345678, 98765, 987654, 9876543, 99999, 999999, 9999999, 111111, 2147483647}
)


def clean_price(value: int | float | None) -> int | None:
    """Цена CZK или None, если это заглушка («1 Kč», «123456», 2^31−1 …)."""
    if value is None or value != value:  # None или NaN
        return None
    v = int(value)
    if v < MIN_REAL_PRICE or v > MAX_REAL_PRICE or v in PLACEHOLDER_PRICES:
        return None
    return v


# --- Аналитика ---------------------------------------------------------------
FAST_SALE_HOURS = (24, 48)  # пороги «ликвидности»
MIN_HOURS_FOR_VPH = 0.5  # меньший интервал даёт шумный VPH
