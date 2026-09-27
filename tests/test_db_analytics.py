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
    assert db.table_counts(path) == {"listings": 0, "listing_snapshots": 0}


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
    assert m["fastest_category"] in {"mobil", "pc", "elektro", "auto"}

    kw = analytics.top_keywords_by_vph(df, 10)
    assert 0 < len(kw) <= 10 and kw["avg_vph"].is_monotonic_decreasing
    assert (kw["listings"] >= 2).all()

    turn = analytics.category_turnover(df)
    assert set(turn["category"]) == {"mobil", "pc", "elektro", "auto"}
    assert analytics.price_of_fast_sellers(df)["category"].nunique() == 4
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
