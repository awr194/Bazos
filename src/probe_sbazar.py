"""Разведчик Sbazar.cz: сохраняет несколько страниц и ответов API, чтобы понять структуру сайта.

Ничего не пишет в базу. Делает не больше --max-requests запросов с той же паузой
1,5–3,5 с, что и основной скрейпер, и не открывает пути, запрещённые в robots.txt.

    python -m src.probe_sbazar                    # по умолчанию поиск «iphone»
    python -m src.probe_sbazar --query "škoda octavia"

Результат — папка data/probe/sbazar_<время>/ и архив рядом с ней (.zip), который
нужно прислать для разработки полноценного сборщика.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from selectolax.parser import HTMLParser

from src import config
from src.scraper import BazosClient

log = logging.getLogger("bazos.probe_sbazar")

BASE_URL = "https://www.sbazar.cz/"
MAX_BODY_BYTES = 2_000_000  # сохраняем не больше 2 МБ на документ
MAX_BUNDLES = 4  # сколько JS-бандлов просмотреть в поисках адресов API

# Кандидаты адресов API — догадки; разведчик проверит, какие существуют на самом деле.
API_CANDIDATES: list[tuple[str, str]] = [
    ("api_search", "api/v1/items/search?phrase={q}&offset=0&limit=20"),
    ("api_items", "api/v1/items?phrase={q}&offset=0&limit=20"),
    ("api_categories", "api/v1/categories"),
    ("api_categories_tree", "api/v1/categories/tree"),
]
# Слова, по которым ищем счётчик просмотров, время публикации и тип продавца.
MARKERS: dict[str, list[str]] = {
    "views": ["zobrazení", "zobrazeni", "shlédnutí", "vidělo", "view_count", "viewcount", "views"],
    "created": ["create_date", "created", "vloženo", "vlozeno", "datum vložení", "published"],
    "seller": ["premise", "firma", "user_id", "seller", "prodejce", "shop"],
    "price": ["price", "cena", "kč"],
    "removed": ["neexistuje", "smazán", "smazan", "byl odstraněn", "nebyl nalezen"],
}

API_PATH_RE = re.compile(r"""["'`]((?:https?://[^"'`\s]*?)?/api/[^"'`\s]{2,200})["'`]""")
DETAIL_HREF_RE = re.compile(r"/detail/|/inzerat/", re.IGNORECASE)


# --- Разбор сохранённых ответов (чистые функции) -------------------------------------
def extract_embedded_json(html: str) -> list[dict[str, Any]]:
    """Данные, встроенные в страницу: <script type="application/json">, __NEXT_DATA__, JSON-LD,
    а также присваивания вида window.__STATE__ = {...}."""
    found: list[dict[str, Any]] = []
    tree = HTMLParser(html)
    for node in tree.css("script"):
        attrs = node.attributes
        body = (node.text() or "").strip()
        if not body:
            continue
        kind = (attrs.get("type") or "").lower()
        label = attrs.get("id") or kind or "script"
        if kind in ("application/json", "application/ld+json") or attrs.get("id") == "__NEXT_DATA__":
            found.append(_parse_json(label, body))
            continue
        for m in re.finditer(r"(window\.[\w$]+|__[A-Z_]+__)\s*=\s*(\{.*?\})\s*;?\s*(?:</|$|\n)", body, re.S):
            found.append(_parse_json(m.group(1), m.group(2)))
    return found


def _parse_json(label: str, text: str) -> dict[str, Any]:
    try:
        return {"label": label, "ok": True, "data": json.loads(text)}
    except (ValueError, TypeError):
        return {"label": label, "ok": False, "raw": text[:5000]}


def json_outline(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 6) -> list[str]:
    """Схема JSON: пути ключей с типом и примером значения — чтобы увидеть поля без всего файла."""
    lines: list[str] = []
    if depth > max_depth:
        return lines
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:60]:
            lines.extend(json_outline(v, f"{prefix}.{k}" if prefix else str(k), depth + 1, max_depth))
    elif isinstance(obj, list):
        lines.append(f"{prefix}[] ({len(obj)} эл.)")
        if obj:
            lines.extend(json_outline(obj[0], f"{prefix}[0]", depth + 1, max_depth))
    else:
        sample = repr(obj)[:60]
        lines.append(f"{prefix}: {type(obj).__name__} = {sample}")
    return lines


def find_api_paths(text: str) -> list[str]:
    return sorted({m.group(1) for m in API_PATH_RE.finditer(text)})


def find_links(html: str, base_url: str) -> dict[str, list[str]]:
    """Внутренние ссылки страницы, отдельно — похожие на карточки объявлений."""
    links: set[str] = set()
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).netloc.endswith("sbazar.cz"):
            links.add(url.split("#")[0])
    details = sorted(u for u in links if DETAIL_HREF_RE.search(urlparse(u).path))
    return {"all": sorted(links), "details": details}


def find_script_srcs(html: str, base_url: str) -> list[str]:
    srcs = []
    for node in HTMLParser(html).css("script[src]"):
        url = urljoin(base_url, node.attributes.get("src") or "")
        if urlparse(url).netloc.endswith(("sbazar.cz", "seznam.cz", "szn.cz")):
            srcs.append(url)
    return srcs


def scan_markers(text: str) -> dict[str, dict[str, int]]:
    low = text.lower()
    return {
        group: {w: low.count(w) for w in words if low.count(w)} for group, words in MARKERS.items()
    }


# --- Разведка -----------------------------------------------------------------------
class Probe:
    def __init__(self, out_dir: Path, client: BazosClient, max_requests: int) -> None:
        self.out_dir = out_dir
        self.client = client
        self.max_requests = max_requests
        self.requests = 0
        self.robots: RobotFileParser | None = None
        self.report: dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "fetches": [],
        }

    def allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch("*", url)

    def fetch(self, name: str, url: str, as_json: bool = False) -> str | None:
        """Скачивает url, сохраняет тело и заносит итог в отчёт. Возвращает текст или None."""
        entry: dict[str, Any] = {"name": name, "url": url}
        self.report["fetches"].append(entry)
        if self.requests >= self.max_requests:
            entry["skipped"] = "лимит запросов"
            return None
        if not self.allowed(url):
            entry["skipped"] = "запрещено robots.txt"
            log.info("Пропуск (robots.txt): %s", url)
            return None
        self.requests += 1
        headers = {"Accept": "application/json, text/plain, */*"} if as_json else None
        resp = self.client.get(url, headers=headers)
        if resp is None:
            entry["error"] = "нет ответа"
            return None
        text = resp.text
        ctype = resp.headers.get("content-type", "")
        entry.update(
            status=resp.status_code,
            final_url=str(resp.url),
            content_type=ctype,
            bytes=len(resp.content),
            markers=scan_markers(text),
        )
        ext = ".json" if "json" in ctype else ".html" if "html" in ctype else ".txt"
        path = self.out_dir / f"{name}{ext}"
        path.write_text(text[:MAX_BODY_BYTES], encoding="utf-8")
        entry["saved_as"] = path.name
        log.info(
            "%s → HTTP %s, %s, %d байт", name, resp.status_code, ctype.split(";")[0], len(resp.content)
        )
        if "json" in ctype:
            try:
                (self.out_dir / f"{name}.outline.txt").write_text(
                    "\n".join(json_outline(resp.json())), encoding="utf-8"
                )
            except ValueError:
                entry["json_error"] = True
        return text

    def analyze_html(self, name: str, html: str, base_url: str) -> dict[str, list[str]]:
        """Встроенный JSON, ссылки и адреса API со страницы; всё сохраняется рядом."""
        embedded = extract_embedded_json(html)
        for i, block in enumerate(embedded):
            if block["ok"]:
                (self.out_dir / f"{name}.embedded{i}.outline.txt").write_text(
                    "\n".join(json_outline(block["data"])), encoding="utf-8"
                )
                dumped = json.dumps(block["data"], ensure_ascii=False, indent=1)
                (self.out_dir / f"{name}.embedded{i}.json").write_text(
                    dumped[:MAX_BODY_BYTES], encoding="utf-8"
                )
        links = find_links(html, base_url)
        (self.out_dir / f"{name}.links.txt").write_text("\n".join(links["all"]), encoding="utf-8")
        api = find_api_paths(html)
        self.report.setdefault("pages", {})[name] = {
            "embedded_json": [{"label": b["label"], "parsed": b["ok"]} for b in embedded],
            "links": len(links["all"]),
            "detail_links": links["details"][:20],
            "api_paths": api,
        }
        return {"details": links["details"], "api": api}

    def run(self, query: str) -> dict[str, Any]:
        self.out_dir.mkdir(parents=True, exist_ok=True)

        robots_txt = self.fetch("robots", urljoin(BASE_URL, "robots.txt"))
        if robots_txt and self.report["fetches"][-1].get("status") == 200:
            self.robots = RobotFileParser()
            self.robots.parse(robots_txt.splitlines())

        api_paths: set[str] = set()
        detail_links: list[str] = []

        home = self.fetch("home", BASE_URL)
        if home:
            res = self.analyze_html("home", home, BASE_URL)
            api_paths.update(res["api"])
            detail_links += res["details"]
            # JS-бандлы: сохраняем только найденные в них адреса API, не сами файлы.
            bundle_api: dict[str, list[str]] = {}
            for i, src in enumerate(find_script_srcs(home, BASE_URL)[:MAX_BUNDLES]):
                text = self.fetch(f"bundle{i}", src)
                if text:
                    (self.out_dir / f"bundle{i}.js").unlink(missing_ok=True)
                    (self.out_dir / f"bundle{i}.txt").unlink(missing_ok=True)
                    bundle_api[src] = find_api_paths(text)
                    api_paths.update(bundle_api[src])
            (self.out_dir / "bundles.api_paths.json").write_text(
                json.dumps(bundle_api, ensure_ascii=False, indent=1), encoding="utf-8"
            )

        search_url = urljoin(BASE_URL, f"hledej/{quote(query)}")
        search = self.fetch("search", search_url)
        if search:
            res = self.analyze_html("search", search, search_url)
            api_paths.update(res["api"])
            detail_links = res["details"] + detail_links

        # Первая карточка объявления — смотрим, есть ли на ней счётчик просмотров.
        if detail_links:
            detail = self.fetch("detail", detail_links[0])
            if detail:
                self.analyze_html("detail", detail, detail_links[0])

        for name, template in API_CANDIDATES:
            self.fetch(name, urljoin(BASE_URL, template.format(q=quote(query))), as_json=True)

        self.report["api_paths_found"] = sorted(api_paths)
        self.report["requests_made"] = self.requests
        (self.out_dir / "summary.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return self.report


def print_summary(report: dict[str, Any], archive: Path | None) -> None:
    print("\n=== Разведка Sbazar.cz ===")
    for f in report["fetches"]:
        if "skipped" in f:
            status = f"пропущено: {f['skipped']}"
        elif "error" in f:
            status = f["error"]
        else:
            status = f"HTTP {f['status']}, {f['content_type'].split(';')[0] or '?'}, {f['bytes']:,} байт"
        views = f.get("markers", {}).get("views")
        print(f"  {f['name']:<20} {status}" + (f"  | просмотры: {views}" if views else ""))
    print(f"Найдено адресов API: {len(report.get('api_paths_found', []))}")
    for p in report.get("api_paths_found", [])[:15]:
        print(f"  {p}")
    for name, page in report.get("pages", {}).items():
        labels = [b["label"] for b in page["embedded_json"]] or "нет"
        print(
            f"{name}: ссылок {page['links']}, карточек {len(page['detail_links'])}, встроенный JSON: {labels}"
        )
    print(f"Запросов сделано: {report.get('requests_made', 0)}")
    if archive:
        print(f"\nПришлите этот файл: {archive}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.probe_sbazar", description="Разведка структуры Sbazar.cz"
    )
    p.add_argument("--query", default="iphone", help="поисковый запрос для пробной выдачи")
    p.add_argument("--out", type=Path, default=config.DATA_DIR / "probe", help="куда сохранить результаты")
    p.add_argument("--max-requests", type=int, default=15, help="предел числа запросов")
    p.add_argument("--no-zip", action="store_true", help="не упаковывать результат в .zip")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    out_dir = args.out / f"sbazar_{datetime.now():%Y%m%d_%H%M%S}"
    with BazosClient() as client:
        report = Probe(out_dir, client, args.max_requests).run(args.query)
    archive = None if args.no_zip else Path(shutil.make_archive(str(out_dir), "zip", out_dir))
    print_summary(report, archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
