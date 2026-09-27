"""Аналитика спроса: VPH, ликвидность (время до продажи), ключевые слова и ценовая эластичность.

Определения:
    VPH (Views Per Hour) = (views_now - views_initial) / hours_passed,
        где интервал берётся между первым и последним снимком с известными просмотрами.
    Время жизни (lifetime_h) = removed_at - first_seen для снятых объявлений.
    Ликвидность = доля объявлений, снятых быстрее 24 / 48 часов.
    Demand score = VPH * (1 + log10(1 + views_current)) — VPH с поправкой на общий интерес.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src import config
from src.db import get_connection

LISTINGS_SQL = """
WITH snaps AS (
    SELECT listing_id,
           MIN(captured_at) AS first_snap_at,
           MAX(captured_at) AS last_snap_at,
           COUNT(*)         AS snapshots
      FROM listing_snapshots
     WHERE views IS NOT NULL
     GROUP BY listing_id
)
SELECT l.*,
       s.first_snap_at, s.last_snap_at, COALESCE(s.snapshots, 0) AS snapshots,
       (SELECT views FROM listing_snapshots x
         WHERE x.listing_id = l.id AND x.captured_at = s.first_snap_at AND x.views IS NOT NULL
         LIMIT 1) AS views_first,
       (SELECT views FROM listing_snapshots x
         WHERE x.listing_id = l.id AND x.captured_at = s.last_snap_at AND x.views IS NOT NULL
         ORDER BY x.id DESC LIMIT 1) AS views_last
  FROM listings l
  LEFT JOIN snaps s ON s.listing_id = l.id
"""

# Слова, которые не несут смысла как «ключевое слово спроса».
STOPWORDS = {
    "and",
    "the",
    "for",
    "pro",
    "nový",
    "nova",
    "nové",
    "nová",
    "novy",
    "prodám",
    "prodam",
    "koupím",
    "koupim",
    "stav",
    "velmi",
    "dobrý",
    "dobry",
    "super",
    "top",
    "cena",
    "záruka",
    "zaruka",
    "sada",
    "černý",
    "cerny",
    "bílý",
    "bily",
    "kombi",
    "gen",
    "generace",
    "edition",
    "nerozbalený",
    "baterie",
    "ovladače",
    "sluchátka",
    # глаголы и обороты из объявлений «куплю»
    "koupíme",
    "koupime",
    "sháním",
    "shanim",
    "sháníme",
    "hledám",
    "hledam",
    "hledáme",
    "poptávám",
    "poptavam",
    "vykoupím",
    "vykoupim",
    "výkup",
    "vada",
    "vadou",
    "rozumná",
    "rozumna",
    "platba",
    "ihned",
    "vaše",
    "vase",
    "dceru",
    "syna",
    "ii",
    "iii",
}
LISTING_TYPE_LABELS = {"offer": "Продаю", "demand": "Куплю / ищу", "buyout": "Перекупщики"}
TOKEN_RE = re.compile(r"[a-zá-žA-ZÁ-Ž][\wá-žÁ-Ž]+", re.UNICODE)


def load_listings(
    db_path: Path | str | None = None,
    categories: Iterable[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> pd.DataFrame:
    """Загружает объявления с рассчитанными VPH, временем жизни и demand score."""
    with get_connection(db_path) as conn:
        df = pd.read_sql_query(LISTINGS_SQL, conn)
    df = enrich(df)
    cats = list(categories or [])
    if cats:
        df = df[df["category"].isin(cats)]
    if date_from is not None:
        df = df[df["first_seen"].dt.date >= date_from]
    if date_to is not None:
        df = df[df["first_seen"].dt.date <= date_to]
    return df.reset_index(drop=True)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет вычисляемые колонки к «сырой» выборке listings."""
    df = df.copy()
    for col in ("first_seen", "last_seen", "last_checked", "removed_at", "first_snap_at", "last_snap_at"):
        if col in df:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    df["is_active"] = df["is_active"].astype(bool)
    if "listing_type" not in df:
        df["listing_type"] = "offer"
    df["listing_type"] = df["listing_type"].fillna("offer")

    hours = (df["last_snap_at"] - df["first_snap_at"]).dt.total_seconds() / 3600
    delta = df["views_last"] - df["views_first"]
    vph = delta / hours
    df["hours_tracked"] = hours
    df["vph"] = vph.where(hours >= config.MIN_HOURS_FOR_VPH).clip(lower=0)

    df["lifetime_h"] = ((df["removed_at"] - df["first_seen"]).dt.total_seconds() / 3600).where(
        ~df["is_active"]
    )
    df["sold_24h"] = df["lifetime_h"] < 24
    df["sold_48h"] = df["lifetime_h"] < 48

    views_now = df["views_current"].fillna(0).astype(float)
    df["demand_score"] = (df["vph"] * (1 + np.log10(1 + views_now))).round(2)
    return df


# --- Метрики ------------------------------------------------------------------
def summary_metrics(df: pd.DataFrame) -> dict[str, object]:
    """Карточки дашборда: активные, медианный VPH, самая «быстрая» категория."""
    turnover = category_turnover(df)
    fastest = None
    if not turnover.empty and turnover["avg_lifetime_h"].notna().any():
        fastest = turnover.sort_values("avg_lifetime_h").iloc[0]
    return {
        "total": int(len(df)),
        "active": int(df["is_active"].sum()) if len(df) else 0,
        "removed": int((~df["is_active"]).sum()) if len(df) else 0,
        "median_vph": float(df["vph"].median()) if df["vph"].notna().any() else None,
        "fastest_category": None if fastest is None else fastest["category"],
        "fastest_lifetime_h": None if fastest is None else float(fastest["avg_lifetime_h"]),
    }


def tokenize(title: str) -> list[str]:
    """Нормализованные токены заголовка: нижний регистр, без стоп-слов и коротких слов."""
    tokens = {t.lower() for t in TOKEN_RE.findall(title or "")}
    return sorted(t for t in tokens if len(t) >= 3 and t not in STOPWORDS and not t.isdigit())


def top_keywords_by_vph(df: pd.DataFrame, n: int = 10, min_count: int = 2) -> pd.DataFrame:
    """Топ-N поисковых запросов/ключевых слов с наибольшим средним VPH.

    Источник ключей — поле `query` (если объявление найдено поиском) и слова заголовка.
    `min_count` отсекает слова, встретившиеся в одном объявлении (шум).
    """
    base = df.loc[df["vph"].notna(), ["id", "title", "query", "vph"]]
    if base.empty:
        return pd.DataFrame(columns=["keyword", "avg_vph", "median_vph", "listings"])

    def keys_for(row: pd.Series) -> list[str]:
        keys = set(tokenize(row["title"]))
        if isinstance(row["query"], str) and row["query"].strip():
            keys.add(row["query"].strip().lower())
        return sorted(keys)

    keys = base.apply(keys_for, axis=1)
    exploded = base.assign(keyword=keys).explode("keyword").dropna(subset=["keyword"])
    agg = (
        exploded.groupby("keyword")
        .agg(avg_vph=("vph", "mean"), median_vph=("vph", "median"), listings=("id", "nunique"))
        .query("listings >= @min_count")
        .sort_values(["avg_vph", "listings"], ascending=[False, False])
        .head(n)
        .reset_index()
    )
    agg[["avg_vph", "median_vph"]] = agg[["avg_vph", "median_vph"]].round(2)
    return agg


def top_queries_by_vph(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """То же, но строго по сохранённым поисковым запросам (`--query`)."""
    base = df[df["query"].notna() & df["vph"].notna()]
    if base.empty:
        return pd.DataFrame(columns=["query", "avg_vph", "listings"])
    return (
        base.groupby("query")
        .agg(avg_vph=("vph", "mean"), listings=("id", "nunique"))
        .round({"avg_vph": 2})
        .sort_values("avg_vph", ascending=False)
        .head(n)
        .reset_index()
    )


def category_turnover(df: pd.DataFrame, fast_hours: float = 48) -> pd.DataFrame:
    """Оборачиваемость по категориям; `is_fast` — среднее время жизни < fast_hours."""
    if df.empty:
        return pd.DataFrame(
            columns=[
                "category",
                "listings",
                "removed",
                "avg_lifetime_h",
                "median_lifetime_h",
                "sold_24h_pct",
                "sold_48h_pct",
                "median_vph",
                "is_fast",
            ]
        )
    g = df.groupby("category")
    out = pd.DataFrame(
        {
            "listings": g["id"].count(),
            "removed": g["is_active"].apply(lambda s: int((~s).sum())),
            "avg_lifetime_h": g["lifetime_h"].mean(),
            "median_lifetime_h": g["lifetime_h"].median(),
            "sold_24h_pct": g["sold_24h"].sum() / g["id"].count() * 100,
            "sold_48h_pct": g["sold_48h"].sum() / g["id"].count() * 100,
            "median_vph": g["vph"].median(),
        }
    ).reset_index()
    out["is_fast"] = out["avg_lifetime_h"] < fast_hours
    return out.round(1).sort_values("avg_lifetime_h", na_position="last").reset_index(drop=True)


def fastest_categories(df: pd.DataFrame, fast_hours: float = 48) -> pd.DataFrame:
    """Только категории со средним временем жизни < fast_hours."""
    t = category_turnover(df, fast_hours)
    return t[t["is_fast"]].reset_index(drop=True)


def price_of_fast_sellers(df: pd.DataFrame, fast_hours: float = 48) -> pd.DataFrame:
    """Медианная цена быстро проданных (< fast_hours) против остальных, по категориям."""
    priced = df[df["price_czk"].notna()].copy()
    if priced.empty:
        return pd.DataFrame(
            columns=[
                "category",
                "fast_median_price",
                "other_median_price",
                "fast_count",
                "other_count",
                "price_ratio",
            ]
        )
    priced["fast"] = priced["lifetime_h"] < fast_hours
    rows = []
    for cat, grp in priced.groupby("category"):
        fast, other = grp[grp["fast"]], grp[~grp["fast"]]
        fm = fast["price_czk"].median() if len(fast) else np.nan
        om = other["price_czk"].median() if len(other) else np.nan
        rows.append(
            {
                "category": cat,
                "fast_median_price": fm,
                "other_median_price": om,
                "fast_count": len(fast),
                "other_count": len(other),
                "price_ratio": round(fm / om, 2) if om and not np.isnan(fm) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def price_elasticity(df: pd.DataFrame, buckets: int = 4, fast_hours: float = 48) -> pd.DataFrame:
    """Приближённая «эластичность»: как доля быстрых продаж и VPH меняются по ценовым квартилям.

    Для каждой категории цены делятся на квантильные корзины; если доля продаж < fast_hours
    падает с ростом цены — спрос эластичен по цене. `slope_pct_per_bucket` — наклон
    линейной регрессии доли быстрых продаж по номеру корзины.
    """
    priced = df[df["price_czk"].notna() & (df["price_czk"] > 0)].copy()
    rows = []
    for cat, grp in priced.groupby("category"):
        q = min(buckets, grp["price_czk"].nunique())
        if q < 2:
            continue
        grp = grp.assign(bucket=pd.qcut(grp["price_czk"], q=q, labels=False, duplicates="drop"))
        for b, bg in grp.groupby("bucket"):
            rows.append(
                {
                    "category": cat,
                    "bucket": int(b) + 1,
                    "price_from": int(bg["price_czk"].min()),
                    "price_to": int(bg["price_czk"].max()),
                    "median_price": float(bg["price_czk"].median()),
                    "listings": len(bg),
                    "fast_sale_pct": round(float((bg["lifetime_h"] < fast_hours).mean() * 100), 1),
                    "median_vph": round(float(bg["vph"].median()), 2) if bg["vph"].notna().any() else None,
                }
            )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    slopes = {
        cat: float(np.polyfit(g["bucket"], g["fast_sale_pct"], 1)[0]) if len(g) >= 2 else np.nan
        for cat, g in out.groupby("category")
    }
    out["slope_pct_per_bucket"] = out["category"].map(slopes).round(1)
    return out


def hottest_items(df: pd.DataFrame, n: int = 15) -> pd.DataFrame:
    """Топ-N объявлений по VPH."""
    return df[df["vph"].notna()].nlargest(n, "vph").reset_index(drop=True)


# --- Спрос против предложения ------------------------------------------------------
def offers(df: pd.DataFrame) -> pd.DataFrame:
    """Только объявления о продаже."""
    return df[df["listing_type"] == "offer"]


def wanted(df: pd.DataFrame) -> pd.DataFrame:
    """Только объявления покупателей («Koupím / Sháním / Hledám»), без перекупщиков."""
    return df[df["listing_type"] == "demand"]


def supply_demand_by_category(df: pd.DataFrame) -> pd.DataFrame:
    """По категориям: сколько продают и сколько ищут, и цена продавцов против бюджета покупателей.

    `demand_per_100_offers` — сколько объявлений «куплю» приходится на 100 объявлений «продаю»
    в собранной выборке. `budget_to_price` < 1 — покупатели готовы платить меньше, чем просят.
    """
    cols = [
        "category",
        "offers",
        "wanted",
        "buyouts",
        "demand_per_100_offers",
        "median_offer_price",
        "median_budget",
        "budget_to_price",
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for cat, g in df.groupby("category"):
        o, w = offers(g), wanted(g)
        op, wb = o["price_czk"].median(), w["price_czk"].median()
        rows.append(
            {
                "category": cat,
                "offers": len(o),
                "wanted": len(w),
                "buyouts": int((g["listing_type"] == "buyout").sum()),
                "demand_per_100_offers": round(len(w) / len(o) * 100, 1) if len(o) else np.nan,
                "median_offer_price": op,
                "median_budget": wb,
                "budget_to_price": round(wb / op, 2) if op and not np.isnan(wb) else np.nan,
            }
        )
    return pd.DataFrame(rows, columns=cols)


def _keyword_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Одна строка на пару (объявление, ключевое слово заголовка)."""
    if df.empty:
        return pd.DataFrame(columns=["id", "keyword", "listing_type", "vph", "price_czk", "is_active"])
    base = df[["id", "title", "listing_type", "vph", "price_czk", "is_active"]].copy()
    base["keyword"] = base["title"].map(tokenize)
    return base.explode("keyword").dropna(subset=["keyword"])


def supply_demand_by_keyword(df: pd.DataFrame, n: int = 15, min_wanted: int = 1) -> pd.DataFrame:
    """Что ищут покупатели и сколько на это предложений.

    `wanted` — объявлений «куплю» с этим словом, `offers` — объявлений «продаю»,
    `wanted_per_offer` — напряжённость спроса (>1 — ищут больше, чем продают),
    `offer_vph` — средний VPH предложений с этим словом (скрытый спрос по просмотрам).
    """
    rows = _keyword_rows(df[df["listing_type"].isin(["offer", "demand"])])
    cols = [
        "keyword",
        "wanted",
        "offers",
        "wanted_per_offer",
        "offer_vph",
        "median_offer_price",
        "median_budget",
    ]
    if rows.empty or not (rows["listing_type"] == "demand").any():
        return pd.DataFrame(columns=cols)
    w = rows[rows["listing_type"] == "demand"].groupby("keyword")
    o = rows[rows["listing_type"] == "offer"].groupby("keyword")
    out = pd.DataFrame(
        {
            "wanted": w["id"].nunique(),
            "median_budget": w["price_czk"].median(),
        }
    ).join(
        pd.DataFrame(
            {
                "offers": o["id"].nunique(),
                "offer_vph": o["vph"].mean(),
                "median_offer_price": o["price_czk"].median(),
            }
        ),
        how="left",
    )
    out = out[out["wanted"] >= min_wanted].reset_index(names="keyword")
    out["offers"] = out["offers"].fillna(0).astype(int)
    out["wanted_per_offer"] = (out["wanted"] / out["offers"].replace(0, np.nan)).round(2)
    out["offer_vph"] = out["offer_vph"].round(2)
    out = out.sort_values(["wanted", "wanted_per_offer"], ascending=[False, False], na_position="first")
    return out[cols].head(n).reset_index(drop=True)


def market_counts_latest(db_path: Path | str | None = None) -> pd.DataFrame:
    """Последний замер общего числа объявлений (python -m src.scraper --count) по рубрике и запросу."""
    sql = """
        SELECT category, kind, query, total, captured_at
          FROM market_counts m
         WHERE captured_at = (
               SELECT MAX(captured_at) FROM market_counts x
                WHERE x.category = m.category AND x.kind = m.kind
                  AND COALESCE(x.query, '') = COALESCE(m.query, ''))
         ORDER BY category, kind, query
    """
    with get_connection(db_path) as conn:
        return pd.read_sql_query(sql, conn)


def market_balance(counts: pd.DataFrame) -> pd.DataFrame:
    """Сводка замеров: всего объявлений в рубрике и результатов поиска по словам спроса."""
    cols = ["category", "offers_total", "demand_hits", "demand_per_1000"]
    if counts.empty:
        return pd.DataFrame(columns=cols)
    offer = counts[counts["kind"] == "offer"].groupby("category")["total"].max()
    # Запросы спроса пересекаются («koupím» и «sháním» в одном объявлении) — берём максимум, не сумму.
    demand = counts[counts["kind"] == "demand"].groupby("category")["total"].max()
    out = pd.DataFrame({"offers_total": offer, "demand_hits": demand}).rename_axis("category").reset_index()
    out["demand_per_1000"] = (out["demand_hits"] / out["offers_total"] * 1000).round(1)
    return out[cols]


if __name__ == "__main__":
    data = load_listings()
    print(summary_metrics(data))
    print("\nТоп ключевых слов по VPH:\n", top_keywords_by_vph(data).to_string(index=False))
    print("\nОборачиваемость категорий:\n", category_turnover(data).to_string(index=False))
    print("\nЦена быстрых продаж:\n", price_of_fast_sellers(data).to_string(index=False))
    print("\nСпрос и предложение по категориям:\n", supply_demand_by_category(data).to_string(index=False))
    print("\nЧто ищут покупатели:\n", supply_demand_by_keyword(data).to_string(index=False))
