"""Per-source fetchers.

Each fetcher takes its config entry (a dict with at least "name" and "url")
and returns a list of RawArticle. Each site's logic is self-contained so
breakage in any one source stays isolated to its own function — a fetcher
that fails logs a warning and returns [] rather than raising.

Regulatory/news sources publish structured data as embedded JSON, DataTables
AJAX endpoints, or plain server-rendered HTML tables — none of them need a
real browser to read (confirmed by inspecting each site's actual network
traffic), except BSE, which sits behind Akamai bot-detection that requires
JS execution to mint a valid session cookie; a plain HTTP client gets an
empty (not even an error) response, so it stays stubbed rather than silently
returning nothing that looks like "no BSE news this week".
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from time import mktime
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

from process.schema import RawArticle

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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


def _dedupe_by_url(articles: list[RawArticle]) -> list[RawArticle]:
    seen: set[str] = set()
    result = []
    for a in articles:
        if a.url in seen:
            continue
        seen.add(a.url)
        result.append(a)
    return result


# ---------------------------------------------------------------------------
# General news scrapers (HTML). Each site's listing page markup was verified
# live before writing these selectors; if a site redesigns, this is the one
# function to fix.
# ---------------------------------------------------------------------------

def fetch_economic_times(source_cfg: dict) -> list[RawArticle]:
    name = source_cfg["name"]
    url = source_cfg.get(
        "url", "https://economictimes.indiatimes.com/topic/mergers-and-acquisitions"
    )
    articles: list[RawArticle] = []
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Economic Times fetch failed (%s): %s", url, exc)
        return articles

    soup = BeautifulSoup(resp.content, "html.parser")
    today = date.today()
    for a in soup.select("h2 a[href]"):
        href = a["href"]
        # /articleshow/ is a real story; /videoshow/, /slideshow/ aren't
        # text articles classify.py can usefully read.
        if "/articleshow/" not in href:
            continue
        headline = a.get_text(strip=True)
        if not headline:
            continue
        link = href if href.startswith("http") else f"https://economictimes.indiatimes.com{href}"
        articles.append(
            RawArticle.create(source=name, headline=headline, url=link, published_date=today)
        )
    return _dedupe_by_url(articles)


def fetch_mint(source_cfg: dict) -> list[RawArticle]:
    name = source_cfg["name"]
    url = source_cfg.get("url", "https://www.livemint.com/companies")
    articles: list[RawArticle] = []
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Mint fetch failed (%s): %s", url, exc)
        return articles

    soup = BeautifulSoup(resp.content, "html.parser")
    today = date.today()
    for a in soup.select("h2 a[href]"):
        href = a["href"]
        headline = a.get_text(strip=True)
        if not headline or not href.startswith("http"):
            continue
        articles.append(
            RawArticle.create(source=name, headline=headline, url=href, published_date=today)
        )
    return _dedupe_by_url(articles)


def fetch_business_standard(source_cfg: dict) -> list[RawArticle]:
    name = source_cfg["name"]
    url = source_cfg.get(
        "url", "https://www.business-standard.com/topic/mergers-acquisitions"
    )
    articles: list[RawArticle] = []
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Business Standard fetch failed (%s): %s", url, exc)
        return articles

    soup = BeautifulSoup(resp.content, "html.parser")
    today = date.today()
    for a in soup.select("a.smallcard-title[href]"):
        headline = a.get_text(strip=True)
        href = a["href"]
        if not headline or not href.startswith("http"):
            continue
        articles.append(
            RawArticle.create(source=name, headline=headline, url=href, published_date=today)
        )
    return _dedupe_by_url(articles)


def fetch_moneycontrol(source_cfg: dict) -> list[RawArticle]:
    """Moneycontrol server-renders its deals page props (including the
    Mergers & Acquisitions / Private Equity / Venture Capital section lists)
    into a Next.js __NEXT_DATA__ JSON blob — reading that directly is far
    more robust than scraping the rendered markup."""
    name = source_cfg["name"]
    url = source_cfg.get("url", "https://www.moneycontrol.com/deals-and-mergers/")
    articles: list[RawArticle] = []
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Moneycontrol fetch failed (%s): %s", url, exc)
        return articles

    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S
    )
    if not match:
        logger.warning("Moneycontrol: __NEXT_DATA__ block not found, page may have changed")
        return articles

    try:
        data = json.loads(match.group(1))
        sections = data["props"]["pageProps"]["dealsData"]["section"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Moneycontrol: could not parse deals data: %s", exc)
        return articles

    today = date.today()
    # section_2/3/4 are Mergers and Acquisitions / Private Equity / Venture
    # Capital as of 2026-09-16 — matched by name rather than key in case the
    # numbering shifts.
    wanted = {"Mergers and Acquisitions", "Private Equity", "Venture Capital"}
    for section in sections.values():
        if section.get("section_name") not in wanted:
            continue
        for item in section.get("list", []):
            headline = item.get("headline")
            weburl = item.get("weburl")
            if not headline or not weburl:
                continue
            articles.append(
                RawArticle.create(
                    source=name,
                    headline=headline,
                    url=weburl,
                    published_date=today,
                    raw_text=item.get("intro", ""),
                )
            )
    return _dedupe_by_url(articles)


# ---------------------------------------------------------------------------
# Regulatory/official sources.
# ---------------------------------------------------------------------------

def fetch_bse_announcements(source_cfg: dict) -> list[RawArticle]:
    """BSE's announcement API sits behind Akamai bot-detection: the session
    cookie it requires (_abck/bm_sz-style) is only issued after real browser
    JS execution, which a plain HTTP client can't do. A bare request gets a
    200 with an empty body rather than an error — verified live on
    2026-09-16 — so this stays unimplemented rather than silently reporting
    zero BSE announcements every week as if that were real data. Would need
    a headless-browser dependency (e.g. Playwright) to do properly."""
    logger.info("fetch_bse_announcements: blocked by BSE's bot protection, skipping")
    return []


def fetch_nse_announcements(source_cfg: dict) -> list[RawArticle]:
    """NSE's corporate-announcements API works with a plain requests.Session
    once it's picked up the cookies from a normal page load first (verified
    live — no headless browser needed, unlike BSE). Announcements carry a
    controlled-vocabulary category field; filtered to the categories that
    are actually M&A/control-transaction relevant so classify.py isn't
    spending its budget on board-meeting and analyst-call notices."""
    name = source_cfg["name"]
    relevant_categories = {
        "Acquisition",
        "Amalgamation/Merger",
        "Scheme of Arrangement",
        "Disclosure under SEBI Takeover Regulations",
        "Public Announcement-Open Offer",
        "Sale or disposal",
        "Other Restructuring",
    }
    articles: list[RawArticle] = []
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        session.get(
            "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
            timeout=20,
        )
        session.headers.update({"Accept": "application/json"})
        today = date.today()
        week_ago = today - timedelta(days=7)
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements",
            params={
                "index": "equities",
                "from_date": week_ago.strftime("%d-%m-%Y"),
                "to_date": today.strftime("%d-%m-%Y"),
            },
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("NSE announcements fetch failed: %s", exc)
        return articles

    for item in items:
        if item.get("desc") not in relevant_categories:
            continue
        company = item.get("sm_name", "")
        text = item.get("attchmntText") or item.get("desc", "")
        headline = f"{company}: {text}" if company else text
        pdf_url = item.get("attchmntFile")
        if not pdf_url:
            continue
        published = None
        try:
            published = datetime.strptime(item["an_dt"][:11], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            published = today
        articles.append(
            RawArticle.create(
                source=name,
                headline=headline[:300],
                url=pdf_url,
                published_date=published,
                raw_text=f"NSE filing category: {item.get('desc')}. Company: {company}.",
            )
        )
    return _dedupe_by_url(articles)


def fetch_cci_combinations(source_cfg: dict) -> list[RawArticle]:
    """CCI publishes combination (M&A) filing/order status as a plain
    DataTables JSON endpoint with no auth or session needed at all —
    verified live. Only the most recent page is fetched (sorted newest
    first) rather than the full multi-thousand-row history."""
    name = source_cfg["name"]
    url = "https://www.cci.gov.in/combination/orders-section31"
    # This DataTables endpoint 500s unless every column's search/orderable
    # spec is present — it validates the shape of a real server-side
    # DataTables request, not just draw/start/length. Verified live.
    columns = [
        "DT_RowIndex", "combination_no", "party_name", "form_type",
        "notification_date", "order_status", "decision_date",
        "summary_files", "order_files",
    ]
    params: dict = {
        "draw": 1,
        "start": 0,
        "length": 30,
        "order[0][column]": 0,
        "order[0][dir]": "desc",
        "search[value]": "",
        "search[regex]": "false",
    }
    for i, col in enumerate(columns):
        params[f"columns[{i}][data]"] = col
        params[f"columns[{i}][name]"] = col
        params[f"columns[{i}][searchable]"] = "true"
        params[f"columns[{i}][orderable]"] = "true"
        params[f"columns[{i}][search][value]"] = ""
        params[f"columns[{i}][search][regex]"] = "false"

    articles: list[RawArticle] = []
    try:
        resp = requests.get(
            url,
            params=params,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("CCI combinations fetch failed: %s", exc)
        return articles

    for row in rows:
        party_name = re.sub("<[^>]+>", "", row.get("party_name") or "").strip()
        combination_no = row.get("combination_no", "")
        if not party_name or not combination_no:
            continue
        status = row.get("order_status", "")
        headline = f"CCI combination filing: {party_name} ({status})"
        published = None
        try:
            published = datetime.strptime(row["notification_date"], "%d/%m/%Y").date()
        except (KeyError, ValueError, TypeError):
            published = date.today()
        row_id = row.get("id")
        detail_url = (
            f"https://www.cci.gov.in/combination/order/details/summary/{row_id}/0/orders-section31"
            if row_id
            else url
        )
        articles.append(
            RawArticle.create(
                source=name,
                headline=headline,
                url=detail_url,
                published_date=published,
                raw_text=(
                    f"CCI combination case {combination_no}, notifying "
                    f"part(ies): {party_name}. Status: {status}. This is a "
                    f"regulatory filing listing only; specific acquirer/"
                    f"target/deal-size detail is in the linked order "
                    f"documents, not extracted here."
                ),
            )
        )
    return _dedupe_by_url(articles)


def fetch_sebi_sast(source_cfg: dict) -> list[RawArticle]:
    """SEBI's takeover (SAST) "Letter of Offer" listing is a plain
    server-rendered HTML table (legacy JSP site, no auth/JS needed) — one
    row per target company with a filing date. It only names the target
    (the acquirer is inside the filed letter itself, which isn't parsed
    here), but a fresh open-offer filing is still a genuine, high-signal
    "someone is making a play for this company" event worth surfacing."""
    name = source_cfg["name"]
    url = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
    articles: list[RawArticle] = []
    try:
        resp = requests.get(
            url,
            params={"doListing": "yes", "sid": 3, "ssid": 20, "smid": 15},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("SEBI SAST fetch failed: %s", exc)
        return articles

    soup = BeautifulSoup(resp.content, "html.parser")
    for row in soup.select("tr"):
        cells = row.find_all("td")
        if len(cells) != 2:
            continue
        date_text = cells[0].get_text(strip=True)
        link = cells[1].find("a", href=True)
        if not link:
            continue
        company = link.get_text(strip=True)
        href = link["href"]
        if not company or not href.startswith("http"):
            continue
        try:
            published = datetime.strptime(date_text, "%b %d, %Y").date()
        except ValueError:
            published = date.today()
        articles.append(
            RawArticle.create(
                source=name,
                headline=f"{company}: SEBI takeover (SAST) letter of offer filed",
                url=href,
                published_date=published,
                raw_text=(
                    f"SEBI SAST Regulations letter-of-offer filing for target "
                    f"company {company}, filed {date_text}. This is a "
                    f"regulatory listing only; the acquiring party and offer "
                    f"terms are in the linked filing, not extracted here."
                ),
            )
        )
    return _dedupe_by_url(articles)


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
