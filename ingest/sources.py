"""Per-source fetchers.

Each fetcher takes its config entry (a dict with at least "name" and "url")
and returns a list of RawArticle. RSS fetchers are implemented and enabled
by default. Scrape/regulatory fetchers are stubbed — enable them in
config.yaml only once a working implementation lands here, so breakage
in any one source stays isolated to its own function.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from time import mktime
from typing import Optional

import feedparser
import requests

from process.schema import RawArticle

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; RADealRadar/1.0; +https://rungtaadvisors.com)"
)


def _parsed_struct_to_date(struct_time) -> Optional[date]:
    if not struct_time:
        return None
    return datetime.fromtimestamp(mktime(struct_time)).date()


def fetch_rss(source_cfg: dict) -> list[RawArticle]:
    """Generic RSS/Atom fetcher used for VCCircle, Entrackr, Inc42, etc."""
    name = source_cfg["name"]
    url = source_cfg["url"]
    articles: list[RawArticle] = []
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        logger.warning("RSS fetch failed for %s (%s): %s", name, url, exc)
        return articles

    for entry in feed.entries:
        headline = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not headline or not link:
            continue
        published = _parsed_struct_to_date(
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )
        summary = getattr(entry, "summary", "") or ""
        articles.append(
            RawArticle.create(
                source=name,
                headline=headline,
                url=link,
                published_date=published,
                raw_text=summary,
            )
        )
    return articles


# ---------------------------------------------------------------------------
# General news scrapers (HTML) — not yet implemented. Selectors here will
# need periodic maintenance as each site's markup changes; keep each site's
# logic self-contained so one breaking doesn't affect the others.
# ---------------------------------------------------------------------------

def fetch_economic_times(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_economic_times: not yet implemented, skipping")
    return []


def fetch_mint(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_mint: not yet implemented, skipping")
    return []


def fetch_business_standard(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_business_standard: not yet implemented, skipping")
    return []


def fetch_moneycontrol(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_moneycontrol: not yet implemented, skipping")
    return []


# ---------------------------------------------------------------------------
# Regulatory/official sources — need session handling (BSE/NSE) or PDF
# extraction (CCI/SEBI). Not yet implemented.
# ---------------------------------------------------------------------------

def fetch_bse_announcements(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_bse_announcements: not yet implemented, skipping")
    return []


def fetch_nse_announcements(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_nse_announcements: not yet implemented, skipping")
    return []


def fetch_cci_combinations(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_cci_combinations: not yet implemented, skipping")
    return []


def fetch_sebi_sast(source_cfg: dict) -> list[RawArticle]:
    logger.info("fetch_sebi_sast: not yet implemented, skipping")
    return []


# Registry mapping config source name -> fetcher function, used by fetch.py
SCRAPE_FETCHERS = {
    "economic_times": fetch_economic_times,
    "mint": fetch_mint,
    "business_standard": fetch_business_standard,
    "moneycontrol": fetch_moneycontrol,
}

REGULATORY_FETCHERS = {
    "bse_announcements": fetch_bse_announcements,
    "nse_announcements": fetch_nse_announcements,
    "cci_combinations": fetch_cci_combinations,
    "sebi_sast": fetch_sebi_sast,
}
