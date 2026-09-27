"""Работа с SQLite: инициализация схемы и безопасные соединения через контекстные менеджеры.

Каждый вызов `get_connection()` открывает новое соединение и гарантированно закрывает его
(commit при успехе, rollback при исключении), поэтому соединения не «зависают» и не
блокируют базу для дашборда, пока работает скрейпер.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id              TEXT PRIMARY KEY,          -- ID объявления на Bazos
    category        TEXT NOT NULL,             -- mobil / pc / elektro / auto
    query           TEXT,                      -- поисковый запрос, по которому найдено (если был)
    title           TEXT NOT NULL,
    price_czk       INTEGER,                   -- NULL, если цена «Dohodou» / «V textu»
    location        TEXT,
    psc             TEXT,                      -- почтовый индекс (PSČ)
    url             TEXT NOT NULL,
    posted_at       TEXT,                      -- дата публикации (YYYY-MM-DD)
    first_seen      TEXT NOT NULL,             -- UTC, когда скрейпер впервые увидел объявление
    last_seen       TEXT NOT NULL,             -- UTC, последний раз видели активным
    last_checked    TEXT,                      -- UTC, последняя проверка в режиме update
    removed_at      TEXT,                      -- UTC, когда обнаружено удаление
    is_active       INTEGER NOT NULL DEFAULT 1,
    views_initial   INTEGER,                   -- просмотры при первом снимке
    views_current   INTEGER                    -- просмотры при последнем снимке
);

CREATE TABLE IF NOT EXISTS listing_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    captured_at  TEXT NOT NULL,                -- UTC
    views        INTEGER,
    price_czk    INTEGER,
    is_active    INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category);
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active);
CREATE INDEX IF NOT EXISTS idx_snapshots_listing ON listing_snapshots(listing_id, captured_at);
"""

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> str:
    """Текущее время UTC в формате, который хранится в БД."""
    return datetime.now(timezone.utc).strftime(TIME_FMT)


def fmt_ts(dt: datetime) -> str:
    """Преобразует datetime в строку UTC для БД."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(TIME_FMT)


@contextmanager
def get_connection(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Открывает соединение, коммитит при успехе, откатывает при ошибке и всегда закрывает."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # читатели не блокируют писателя
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> Path:
    """Создаёт таблицы и индексы (идемпотентно). Возвращает путь к базе."""
    path = Path(db_path) if db_path else DB_PATH
    with get_connection(path) as conn:
        conn.executescript(SCHEMA)
    return path


def upsert_listing(conn: sqlite3.Connection, item: dict[str, Any], seen_at: str) -> bool:
    """Вставляет новое объявление или обновляет существующее. Возвращает True, если оно новое."""
    exists = conn.execute("SELECT 1 FROM listings WHERE id = ?", (item["id"],)).fetchone()
    if exists:
        conn.execute(
            """
            UPDATE listings
               SET title = ?, price_czk = COALESCE(?, price_czk), location = COALESCE(?, location),
                   psc = COALESCE(?, psc), url = ?, posted_at = COALESCE(?, posted_at),
                   query = COALESCE(query, ?), last_seen = ?, is_active = 1, removed_at = NULL
             WHERE id = ?
            """,
            (
                item["title"],
                item.get("price_czk"),
                item.get("location"),
                item.get("psc"),
                item["url"],
                item.get("posted_at"),
                item.get("query"),
                seen_at,
                item["id"],
            ),
        )
        return False
    conn.execute(
        """
        INSERT INTO listings (id, category, query, title, price_czk, location, psc, url,
                              posted_at, first_seen, last_seen, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            item["id"],
            item["category"],
            item.get("query"),
            item["title"],
            item.get("price_czk"),
            item.get("location"),
            item.get("psc"),
            item["url"],
            item.get("posted_at"),
            seen_at,
            seen_at,
        ),
    )
    return True


def add_snapshot(
    conn: sqlite3.Connection,
    listing_id: str,
    captured_at: str,
    views: int | None,
    price_czk: int | None = None,
    is_active: bool = True,
) -> None:
    """Сохраняет снимок просмотров и обновляет views_initial/views_current в listings."""
    conn.execute(
        "INSERT INTO listing_snapshots (listing_id, captured_at, views, price_czk, is_active)"
        " VALUES (?, ?, ?, ?, ?)",
        (listing_id, captured_at, views, price_czk, int(is_active)),
    )
    if views is not None:
        conn.execute(
            "UPDATE listings SET views_initial = COALESCE(views_initial, ?), views_current = ? WHERE id = ?",
            (views, views, listing_id),
        )


def mark_inactive(conn: sqlite3.Connection, listing_id: str, when: str) -> None:
    """Помечает объявление как снятое (продано/удалено)."""
    conn.execute(
        "UPDATE listings SET is_active = 0, removed_at = COALESCE(removed_at, ?), last_checked = ?"
        " WHERE id = ?",
        (when, when, listing_id),
    )
    conn.execute(
        "INSERT INTO listing_snapshots (listing_id, captured_at, views, price_czk, is_active)"
        " VALUES (?, ?, NULL, NULL, 0)",
        (listing_id, when),
    )


def active_listings(
    db_path: Path | str | None = None, categories: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Возвращает активные объявления (для режима update)."""
    sql = "SELECT id, category, url, price_czk FROM listings WHERE is_active = 1"
    params: list[Any] = []
    cats = list(categories or [])
    if cats:
        sql += f" AND category IN ({','.join('?' * len(cats))})"
        params.extend(cats)
    sql += " ORDER BY COALESCE(last_checked, first_seen)"
    with get_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def table_counts(db_path: Path | str | None = None) -> dict[str, int]:
    """Число строк в таблицах — для быстрой проверки состояния базы."""
    with get_connection(db_path) as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("listings", "listing_snapshots")
        }


if __name__ == "__main__":
    p = init_db()
    print(f"База инициализирована: {p}")
    print(table_counts(p))
