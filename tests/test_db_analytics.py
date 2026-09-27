"""Тесты инициализации SQLite, демо-данных и аналитики."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from src import analytics, db, scraper


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "seed.db"
    n = scraper.seed_sample(path)
    return path, n


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    db.init_db(path)
    db.init_db(path)
    with db.get_connection(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"listings", "listing_snapshots"} <= tables
    assert db.table_counts(path) == {"listings": 0, "listing_snapshots": 0, "market_counts": 0, "taxonomy": 0}


def test_connection_is_closed_and_rolled_back(tmp_path):
    path = db.init_db(tmp_path / "x.db")
    with pytest.raises(RuntimeError), db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO listings (id, category, title, url, first_seen, last_seen)"
            " VALUES ('1','pc','t','u','2026-01-01','2026-01-01')"
        )
        raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # соединение закрыто
    assert db.table_counts(path)["listings"] == 0  # транзакция откатилась


def test_seed_sample(seeded):
    path, n = seeded
    assert 30 <= n <= 50
    counts = db.table_counts(path)
    assert counts["listings"] == n and counts["listing_snapshots"] > n
    # Повторный сид не дублирует записи
    scraper.seed_sample(path)
    assert db.table_counts(path)["listings"] == n


def test_analytics_on_seed(seeded):
    path, n = seeded
    df = analytics.load_listings(path)
    assert len(df) == n
    assert df["vph"].notna().sum() > n // 2
    assert (df["vph"].dropna() >= 0).all()
    assert df.loc[~df["is_active"], "lifetime_h"].notna().all()

    m = analytics.summary_metrics(df)
    assert m["active"] + m["removed"] == n and m["median_vph"] > 0
    assert m["fastest_category"] in set(df["category"])

    kw = analytics.top_keywords_by_vph(df, 10)
    assert 0 < len(kw) <= 10 and kw["avg_vph"].is_monotonic_decreasing
    assert (kw["listings"] >= 2).all()

    turn = analytics.category_turnover(df)
    assert {"mobil", "pc", "elektro", "auto", "motorky", "sport"} <= set(turn["category"])
    assert analytics.price_of_fast_sellers(df)["category"].nunique() >= 4

    by_tag = analytics.category_turnover(df, by="tag")
    assert "mobil/apple" in set(by_tag["tag"]) and by_tag["listings"].sum() == n
    assert not analytics.price_elasticity(df).empty
    assert len(analytics.hottest_items(df, 15)) == 15

    only_pc = analytics.load_listings(path, categories=["pc"])
    assert set(only_pc["category"]) == {"pc"}


def test_vph_formula():
    raw = pd.DataFrame(
        [
            {
                "id": "1",
                "category": "pc",
                "query": None,
                "title": "RTX 3080",
                "price_czk": 10000,
                "is_active": 1,
                "first_seen": "2026-09-01 00:00:00",
                "last_seen": "2026-09-01 10:00:00",
                "last_checked": None,
                "removed_at": None,
                "views_current": 150,
                "first_snap_at": "2026-09-01 00:00:00",
                "last_snap_at": "2026-09-01 10:00:00",
                "views_first": 50,
                "views_last": 150,
            }
        ]
    )
    out = analytics.enrich(raw)
    assert out.loc[0, "vph"] == pytest.approx(10.0)
    assert pd.isna(out.loc[0, "lifetime_h"])


def test_migration_adds_listing_type(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:  # схема первой версии, без listing_type
        conn.execute(
            "CREATE TABLE listings (id TEXT PRIMARY KEY, category TEXT NOT NULL, query TEXT, "
            "title TEXT NOT NULL, price_czk INTEGER, location TEXT, psc TEXT, url TEXT NOT NULL, "
            "posted_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_checked TEXT, "
            "removed_at TEXT, is_active INTEGER NOT NULL DEFAULT 1, views_initial INTEGER, "
            "views_current INTEGER)"
        )
        conn.execute(
            "INSERT INTO listings (id, category, title, url, first_seen, last_seen) "
            "VALUES ('1', 'pc', 'RTX', 'u', '2026-01-01', '2026-01-01')"
        )
    conn.close()
    db.init_db(path)
    with db.get_connection(path) as conn:
        row = conn.execute("SELECT listing_type, subcategory FROM listings").fetchone()
        assert row["listing_type"] == "offer" and row["subcategory"] is None


def test_supply_demand_on_seed(seeded):
    path, _ = seeded
    df = analytics.load_listings(path)
    assert set(df["listing_type"]) == {"offer", "demand", "buyout"}

    sd = analytics.supply_demand_by_category(df).set_index("category")
    assert sd["wanted"].sum() == len(analytics.wanted(df)) == 8
    assert sd.loc["auto", "buyouts"] == 1
    assert (sd.loc[["mobil", "pc", "elektro", "auto"], "demand_per_100_offers"] > 0).all()

    kw = analytics.supply_demand_by_keyword(df).set_index("keyword")
    assert kw.loc["iphone", "wanted"] == 2 and kw.loc["iphone", "offers"] >= 1
    assert "koupím" not in kw.index and "sháním" not in kw.index
    # метрики предложения не смешиваются со спросом
    assert (analytics.offers(df)["listing_type"] == "offer").all()


def test_market_balance(tmp_path):
    path = db.init_db(tmp_path / "m.db")
    with db.get_connection(path) as conn:
        db.add_market_count(conn, "2026-09-01 10:00:00", "mobil", "offer", 5000)
        db.add_market_count(conn, "2026-09-02 10:00:00", "mobil", "offer", 6000)
        db.add_market_count(conn, "2026-09-02 10:00:00", "mobil", "demand", 120, "koupím")
        db.add_market_count(conn, "2026-09-02 10:00:00", "mobil", "demand", 90, "sháním")
    latest = analytics.market_counts_latest(path)
    assert len(latest) == 3  # старый замер 5000 отброшен
    bal = analytics.market_balance(latest).set_index("category")
    assert bal.loc["mobil", "offers_total"] == 6000
    assert bal.loc["mobil", "demand_hits"] == 120
    assert bal.loc["mobil", "demand_per_1000"] == 20.0


def test_supply_demand_by_subcategory(seeded):
    path, _ = seeded
    df = analytics.load_listings(path)
    sd = analytics.supply_demand_by_category(df, by="tag").set_index("tag")
    assert sd.loc["auto/skoda", "wanted"] == 2
    assert sd.loc["mobil/apple", "wanted"] == 2


def test_taxonomy(tmp_path):
    path = db.init_db(tmp_path / "t.db")
    with db.get_connection(path) as conn:
        db.upsert_taxonomy(conn, "mobil", "", "Mobily", "https://mobil.bazos.cz/", "2026-09-01 00:00:00")
        db.upsert_taxonomy(
            conn, "mobil", "apple", "Apple", "https://mobil.bazos.cz/apple/", "2026-09-01 00:00:00"
        )
        db.upsert_taxonomy(
            conn, "mobil", "apple", None, "https://mobil.bazos.cz/apple/", "2026-09-02 00:00:00"
        )
    rows = db.list_taxonomy(path, ["mobil"])
    assert [(r["subcategory"], r["name"]) for r in rows] == [("", "Mobily"), ("apple", "Apple")]
