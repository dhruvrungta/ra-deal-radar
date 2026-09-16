"""SQLite storage for raw articles and classified deals.

Two tables:
  - articles: every raw item we've ever fetched, keyed by article_id, so we
    never re-classify the same article twice.
  - deals: classified deals, keyed by Deal.id (a hash of acquirer+target so
    the same deal can be found again week over week and have its `stage`
    updated in place instead of being re-listed as new).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

from process.schema import Deal, RawArticle

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    article_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    headline TEXT NOT NULL,
    url TEXT NOT NULL,
    published_date TEXT,
    raw_text TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    classified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deals (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    headline TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    published_date TEXT NOT NULL,
    acquirer TEXT,
    target TEXT,
    sector TEXT,
    deal_size_usd_m REAL,
    deal_type TEXT NOT NULL,
    cross_border TEXT NOT NULL,
    stage TEXT NOT NULL,
    advisors TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL,
    secondary_sources TEXT NOT NULL DEFAULT '[]',
    first_seen_date TEXT NOT NULL,
    last_updated_date TEXT NOT NULL,
    included_in_digest_weeks TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_deals_published_date ON deals(published_date);
CREATE INDEX IF NOT EXISTS idx_deals_acquirer_target ON deals(acquirer, target);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------

def article_exists(conn: sqlite3.Connection, article_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM articles WHERE article_id = ?", (article_id,)
    ).fetchone()
    return row is not None


def insert_articles(conn: sqlite3.Connection, articles: list[RawArticle]) -> int:
    """Insert new articles, skipping ones already seen. Returns count inserted."""
    inserted = 0
    for a in articles:
        try:
            conn.execute(
                """INSERT INTO articles
                   (article_id, source, headline, url, published_date, raw_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    a.article_id,
                    a.source,
                    a.headline,
                    a.url,
                    a.published_date.isoformat() if a.published_date else None,
                    a.raw_text,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            continue  # already have this article
    return inserted


def get_unclassified_articles(conn: sqlite3.Connection) -> list[RawArticle]:
    rows = conn.execute(
        "SELECT * FROM articles WHERE classified = 0 ORDER BY fetched_at"
    ).fetchall()
    return [
        RawArticle(
            article_id=r["article_id"],
            source=r["source"],
            headline=r["headline"],
            url=r["url"],
            published_date=date.fromisoformat(r["published_date"])
            if r["published_date"]
            else None,
            raw_text=r["raw_text"] or "",
        )
        for r in rows
    ]


def mark_articles_classified(conn: sqlite3.Connection, article_ids: list[str]) -> None:
    conn.executemany(
        "UPDATE articles SET classified = 1 WHERE article_id = ?",
        [(aid,) for aid in article_ids],
    )


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["advisors"] = json.loads(d["advisors"])
    d["secondary_sources"] = json.loads(d["secondary_sources"])
    d["included_in_digest_weeks"] = json.loads(d["included_in_digest_weeks"])
    return d


def get_deal(conn: sqlite3.Connection, deal_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return _row_to_dict(row) if row else None


def find_deals_by_parties(
    conn: sqlite3.Connection, acquirer: Optional[str], target: Optional[str]
) -> list[dict]:
    """Loose candidate lookup for cross-week continuity matching (dedupe.py
    does the fuzzy comparison; this just narrows the search space)."""
    if not acquirer and not target:
        return []
    rows = conn.execute(
        "SELECT * FROM deals WHERE acquirer IS NOT NULL OR target IS NOT NULL"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def insert_deal(conn: sqlite3.Connection, deal: Deal, digest_week: str) -> None:
    conn.execute(
        """INSERT INTO deals
           (id, article_id, headline, source, url, published_date, acquirer,
            target, sector, deal_size_usd_m, deal_type, cross_border, stage,
            advisors, summary, secondary_sources, first_seen_date,
            last_updated_date, included_in_digest_weeks)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            deal.id,
            deal.article_id,
            deal.headline,
            deal.source,
            deal.url,
            deal.published_date.isoformat(),
            deal.acquirer,
            deal.target,
            deal.sector,
            deal.deal_size_usd_m,
            deal.deal_type,
            deal.cross_border,
            deal.stage,
            json.dumps(deal.advisors),
            deal.summary,
            json.dumps([]),
            deal.published_date.isoformat(),
            deal.published_date.isoformat(),
            json.dumps([digest_week]),
        ),
    )


def update_deal_stage(
    conn: sqlite3.Connection,
    deal_id: str,
    new_stage: str,
    updated_date: date,
    digest_week: str,
) -> None:
    existing = get_deal(conn, deal_id)
    if existing is None:
        return
    weeks = existing["included_in_digest_weeks"]
    if digest_week not in weeks:
        weeks.append(digest_week)
    conn.execute(
        """UPDATE deals SET stage = ?, last_updated_date = ?,
           included_in_digest_weeks = ? WHERE id = ?""",
        (new_stage, updated_date.isoformat(), json.dumps(weeks), deal_id),
    )


def add_secondary_source(conn: sqlite3.Connection, deal_id: str, source: str, url: str) -> None:
    existing = get_deal(conn, deal_id)
    if existing is None:
        return
    secondary = existing["secondary_sources"]
    if not any(s["url"] == url for s in secondary):
        secondary.append({"source": source, "url": url})
    conn.execute(
        "UPDATE deals SET secondary_sources = ? WHERE id = ?",
        (json.dumps(secondary), deal_id),
    )


def get_deals_for_week(conn: sqlite3.Connection, digest_week: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM deals").fetchall()
    deals = [_row_to_dict(r) for r in rows]
    return [d for d in deals if digest_week in d["included_in_digest_weeks"]]


def get_deals_in_date_range(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM deals WHERE published_date BETWEEN ? AND ? ORDER BY deal_size_usd_m DESC",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]
