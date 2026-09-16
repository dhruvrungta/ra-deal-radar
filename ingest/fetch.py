"""Orchestrates all ingest sources and persists new raw articles."""

from __future__ import annotations

import logging
import sqlite3

from ingest.sources import (
    REGULATORY_FETCHERS,
    SCRAPE_FETCHERS,
    fetch_rss,
)
from process.schema import RawArticle
from store import db

logger = logging.getLogger(__name__)


def fetch_all(config: dict) -> list[RawArticle]:
    """Runs every enabled source fetcher and returns the combined article list
    (may include duplicates across sources — dedupe.py handles that)."""
    sources_cfg = config.get("sources", {})
    articles: list[RawArticle] = []

    for cfg in sources_cfg.get("rss", []):
        if not cfg.get("enabled", True):
            continue
        fetched = fetch_rss(cfg)
        logger.info("Fetched %d articles from %s (rss)", len(fetched), cfg["name"])
        articles.extend(fetched)

    for cfg in sources_cfg.get("scrape", []):
        if not cfg.get("enabled", False):
            continue
        fetcher = SCRAPE_FETCHERS.get(cfg["name"])
        if fetcher is None:
            logger.warning("No fetcher registered for scrape source %s", cfg["name"])
            continue
        fetched = fetcher(cfg)
        logger.info("Fetched %d articles from %s (scrape)", len(fetched), cfg["name"])
        articles.extend(fetched)

    for cfg in sources_cfg.get("regulatory", []):
        if not cfg.get("enabled", False):
            continue
        fetcher = REGULATORY_FETCHERS.get(cfg["name"])
        if fetcher is None:
            logger.warning("No fetcher registered for regulatory source %s", cfg["name"])
            continue
        fetched = fetcher(cfg)
        logger.info("Fetched %d articles from %s (regulatory)", len(fetched), cfg["name"])
        articles.extend(fetched)

    return articles


def ingest_and_store(config: dict, conn: sqlite3.Connection) -> int:
    """Fetches everything, stores new articles, and returns the count of
    genuinely new articles inserted (already-seen ones are skipped)."""
    articles = fetch_all(config)
    inserted = db.insert_articles(conn, articles)
    logger.info(
        "Ingest complete: %d articles fetched, %d new", len(articles), inserted
    )
    return inserted
