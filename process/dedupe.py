"""Two levels of dedup:

1. Same-week: multiple outlets covering the same deal in this run's batch of
   freshly classified articles. Fuzzy-matched on (acquirer, target, deal size);
   the most detailed article wins as primary, others are recorded as
   secondary sources.

2. Cross-week continuity: a deal already in the database progressing through
   stages (rumored -> announced -> completed). Matched the same way against
   existing DB rows; instead of inserting a new row we update `stage` on the
   existing one and record this week as another week it appeared in.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from process.schema import Deal
from store import db

logger = logging.getLogger(__name__)


@dataclass
class MergedDeal:
    primary: Deal
    secondary_sources: list[dict] = field(default_factory=list)  # [{"source", "url"}]


def _name_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a.lower().strip(), b.lower().strip())


def _names_match(a: str | None, b: str | None, threshold: float) -> bool | None:
    """True/False when both names are present and comparable; None when at
    least one is missing (can't say either way). A substring check on top of
    the fuzzy ratio catches "Groww" vs "Groww's parent, Billionbrains Garage
    Ventures" — very different lengths defeat token_sort_ratio, but one
    plainly contains the other."""
    if not a or not b:
        return None
    an, bn = a.lower().strip(), b.lower().strip()
    if an in bn or bn in an:
        return True
    return _name_similarity(a, b) >= threshold


def _sizes_compatible(a: float | None, b: float | None, tolerance_pct: float) -> bool:
    if a is None or b is None:
        return True  # no info to contradict a match on names
    if a == 0 and b == 0:
        return True
    diff = abs(a - b) / max(a, b, 1e-9)
    return diff <= tolerance_pct


def _same_deal(d1: Deal, d2: Deal, threshold: float, size_tolerance_pct: float) -> bool:
    if not (d1.acquirer or d1.target) or not (d2.acquirer or d2.target):
        return False  # nothing to compare on; treat as distinct
    if not _sizes_compatible(d1.deal_size_usd_m, d2.deal_size_usd_m, size_tolerance_pct):
        return False
    # Target is checked first and, when both sides have one, is decisive
    # either way — a clear target MISMATCH (e.g. "Slayd" vs "Dialflo", two
    # different startups AJVC happened to fund at similarly tiny sizes the
    # same week) must not be overridden by a matching acquirer. Acquirer is
    # only the tiebreaker when target can't be compared on both sides at all
    # (one outlet omits it, or reports a holding company instead).
    target_match = _names_match(d1.target, d2.target, threshold)
    if target_match is not None:
        return target_match
    return bool(_names_match(d1.acquirer, d2.acquirer, threshold))


def _detail_score(d: Deal) -> tuple:
    """Longer summary + presence of advisors/size = more detailed article."""
    return (
        len(d.summary or ""),
        len(d.advisors or []),
        1 if d.deal_size_usd_m is not None else 0,
    )


def merge_same_week(deals: list[Deal], config: dict) -> list[MergedDeal]:
    """Collapses duplicate coverage of the same deal within this run's batch."""
    cfg = config.get("dedupe", {})
    threshold = cfg.get("same_week_similarity_threshold", 82)
    size_tol = cfg.get("deal_size_tolerance_pct", 0.15)

    merged: list[MergedDeal] = []
    for deal in deals:
        placed = False
        for m in merged:
            if _same_deal(m.primary, deal, threshold, size_tol):
                if _detail_score(deal) > _detail_score(m.primary):
                    # new one is more detailed — it becomes primary, old
                    # primary demotes to a secondary source
                    m.secondary_sources.append(
                        {"source": m.primary.source, "url": m.primary.url}
                    )
                    m.primary = deal
                else:
                    m.secondary_sources.append({"source": deal.source, "url": deal.url})
                placed = True
                break
        if not placed:
            merged.append(MergedDeal(primary=deal))

    if len(merged) < len(deals):
        logger.info(
            "Same-week dedupe: %d classified deals collapsed into %d unique deals",
            len(deals),
            len(merged),
        )
    return merged


def resolve_continuity(
    conn: sqlite3.Connection, merged: MergedDeal, config: dict, digest_week: str
) -> str:
    """Writes one merged deal to the DB: either as a brand-new deal, or as a
    stage update to an existing deal it matches. Returns the final deal id
    used in storage (may differ from merged.primary.id if it matched an
    existing DB row under a different id)."""
    cfg = config.get("dedupe", {})
    threshold = cfg.get("same_week_similarity_threshold", 82)
    size_tol = cfg.get("deal_size_tolerance_pct", 0.15)

    deal = merged.primary
    existing_exact = db.get_deal(conn, deal.id)
    if existing_exact is not None:
        final_id = deal.id
        _apply_update(conn, final_id, deal, merged.secondary_sources, digest_week)
        return final_id

    candidates = db.find_deals_by_parties(conn, deal.acquirer, deal.target)
    for cand in candidates:
        cand_as_deal_like = _dict_to_comparable(cand)
        if _same_deal(cand_as_deal_like, deal, threshold, size_tol):
            final_id = cand["id"]
            _apply_update(conn, final_id, deal, merged.secondary_sources, digest_week)
            return final_id

    db.insert_deal(conn, deal, digest_week)
    for sec in merged.secondary_sources:
        db.add_secondary_source(conn, deal.id, sec["source"], sec["url"])
    return deal.id


def _dict_to_comparable(row: dict) -> Deal:
    """Builds a throwaway Deal-shaped object from a DB row for comparison
    purposes only (skips validation of fields we don't need here)."""
    return Deal.model_construct(
        id=row["id"],
        article_id=row["article_id"],
        headline=row["headline"],
        source=row["source"],
        url=row["url"],
        published_date=row["published_date"],
        acquirer=row["acquirer"],
        target=row["target"],
        sector=row["sector"],
        deal_size_usd_m=row["deal_size_usd_m"],
        deal_type=row["deal_type"],
        cross_border=row["cross_border"],
        stage=row["stage"],
        advisors=row["advisors"],
        summary=row["summary"],
    )


def _apply_update(
    conn: sqlite3.Connection,
    deal_id: str,
    new_deal: Deal,
    secondary_sources: list[dict],
    digest_week: str,
) -> None:
    db.update_deal_stage(conn, deal_id, new_deal.stage, new_deal.published_date, digest_week)
    db.add_secondary_source(conn, deal_id, new_deal.source, new_deal.url)
    for sec in secondary_sources:
        db.add_secondary_source(conn, deal_id, sec["source"], sec["url"])


def dedupe_and_store(
    conn: sqlite3.Connection, deals: list[Deal], config: dict, digest_week: str
) -> list[str]:
    """Full dedupe pipeline for a batch of freshly classified deals. Returns
    the list of final deal ids written/updated this run."""
    merged = merge_same_week(deals, config)
    return [resolve_continuity(conn, m, config, digest_week) for m in merged]
