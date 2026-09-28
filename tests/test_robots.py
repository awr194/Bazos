"""Тесты разбора robots.txt и того, что клиент не запрашивает запрещённые адреса."""

from __future__ import annotations

import httpx
import pytest

from src.robots import RobotsRules
from src.scraper import BazosClient

# Фрагмент настоящего robots.txt Bazos (сентябрь 2026).
BAZOS_ROBOTS = """
User-agent: *
Disallow: /search.php
Disallow: /*hledat=
Disallow: /*hlokalita=
Disallow: /*humkreis
Disallow: /*cenaod=
Disallow: /*order=
Disallow: /suggest.php

User-agent: Mediapartners-Google
Disallow:

User-agent: SemrushBot
Disallow: /
"""

# Фрагмент robots.txt Sbazar: всем, кроме поисковиков, закрыт весь сайт.
SBAZAR_ROBOTS = """
User-agent: *
Disallow: /

User-agent: Googlebot
Allow: /
Disallow: /admin/
"""


@pytest.mark.parametrize(
    "path,allowed",
    [
        ("/", True),
        ("/20/", True),
        ("/apple/", True),
        ("/inzerat/224273792/iphone-13.php", True),
        ("/?hledat=iphone&hlokalita=&humkreis=25", False),
        ("/20/?hledat=koup%C3%ADm", False),
        ("/?order=1", False),
        ("/search.php?x=1", False),
        ("/suggest.php", False),
    ],
)
def test_bazos_rules(path, allowed):
    assert RobotsRules.parse(BAZOS_ROBOTS).is_allowed(path) is allowed


def test_only_generic_group_applies():
    # «SemrushBot: Disallow: /» нас не касается, а правила Sbazar для «*» закрывают всё.
    assert RobotsRules.parse(BAZOS_ROBOTS).is_allowed("/auto/")
    sbazar = RobotsRules.parse(SBAZAR_ROBOTS)
    assert not sbazar.is_allowed("/") and not sbazar.is_allowed("/hledej/iphone")


def test_longest_match_and_anchor():
    rules = RobotsRules.parse(
        "User-agent: *\nDisallow: /ulozene\nAllow: /ulozene-nabidky$\nDisallow: /*.pdf$\n"
    )
    assert rules.is_allowed("/ulozene-nabidky")
    assert not rules.is_allowed("/ulozene-nabidky/1")
    assert not rules.is_allowed("/files/cenik.pdf")
    assert rules.is_allowed("/files/cenik.pdf?v=2")
    assert RobotsRules.parse("").is_allowed("/anything")


def test_client_skips_disallowed_urls():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=BAZOS_ROBOTS)
        return httpx.Response(200, text="ok")

    with BazosClient(min_delay=0, max_delay=0, transport=httpx.MockTransport(handler)) as client:
        assert client.get("https://mobil.bazos.cz/", params={"hledat": "iphone"}) is None
        assert client.get("https://mobil.bazos.cz/search.php") is None
        resp = client.get("https://mobil.bazos.cz/20/")
        assert resp is not None and resp.status_code == 200

    assert [u for u in calls if "robots.txt" not in u] == ["https://mobil.bazos.cz/20/"]
    assert sum("robots.txt" in u for u in calls) == 1  # robots.txt загружается один раз на хост


def test_missing_robots_means_no_limits():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="ok")

    with BazosClient(min_delay=0, max_delay=0, transport=httpx.MockTransport(handler)) as client:
        resp = client.get("https://example.cz/?hledat=x")
    assert resp is not None and resp.status_code == 200
