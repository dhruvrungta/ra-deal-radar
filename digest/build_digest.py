"""Groups this week's classified deals into digest sections and renders HTML."""

from __future__ import annotations

import base64
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from store import db

# VC rounds don't get their own section — they're folded into the PE bucket
# below, since this digest is for an M&A/PE advisory firm and a standalone
# VC section isn't the focus.
PE_TYPES = {"pe_buyout", "pe_growth", "pe_minority", "pe_exit", "vc"}


def _bucket_for(deal: dict, min_pe_vc_size: float = 0) -> str | None:
    # Cross-border is a strategic-M&A distinction (a control transaction
    # crossing a border is the notable thing); PE/VC rounds routinely involve
    # foreign investors and get their own section regardless of investor
    # nationality, so they're bucketed by deal_type before cross_border.
    if deal["stage"] == "rumored":
        return "in_talks"
    if deal["deal_type"] in PE_TYPES:
        size = deal.get("deal_size_usd_m")
        # Undisclosed-size deals can't be confirmed to clear the bar, so
        # they're dropped along with ones that are just too small — this
        # section is for substantial PE/VC activity, not noise from every
        # small seed round.
        if size is None or size < min_pe_vc_size:
            return None
        return "pe_buyouts_growth"
    if deal["cross_border"] == "inbound":
        return "cross_border_inbound"
    if deal["cross_border"] == "outbound":
        return "cross_border_outbound"
    return "domestic_ma"


def _format_size(deal: dict) -> str | None:
    v = deal.get("deal_size_usd_m")
    if v is None:
        return None
    if v >= 1000:
        return f"${v / 1000:.2f}B"
    return f"${v:.1f}M"


def _annotate(deal: dict) -> dict:
    deal = dict(deal)
    deal["deal_size_display"] = _format_size(deal)
    return deal


def group_deals(
    deals: list[dict], top_deals_count: int, min_pe_vc_size: float = 0
) -> dict[str, list[dict]]:
    sections: dict[str, list[dict]] = {
        "top_deals": [],
        "domestic_ma": [],
        "cross_border_inbound": [],
        "cross_border_outbound": [],
        "pe_buyouts_growth": [],
        "advisor_mandates": [],
        "in_talks": [],
    }

    annotated = [_annotate(d) for d in deals]

    for d in annotated:
        bucket = _bucket_for(d, min_pe_vc_size)
        if bucket is not None:
            sections[bucket].append(d)

    sized_confirmed = [
        d for d in annotated if d["deal_size_usd_m"] is not None and d["stage"] != "rumored"
    ]
    sections["top_deals"] = sorted(
        sized_confirmed, key=lambda d: d["deal_size_usd_m"], reverse=True
    )[:top_deals_count]

    sections["advisor_mandates"] = [d for d in annotated if d.get("advisors")]

    for key in ("domestic_ma", "cross_border_inbound", "cross_border_outbound",
                "pe_buyouts_growth", "in_talks"):
        sections[key].sort(key=lambda d: d.get("deal_size_usd_m") or 0, reverse=True)

    return sections


def _load_logo_data_uri(logo_path: str | None) -> str | None:
    if not logo_path or not Path(logo_path).exists():
        return None
    data = Path(logo_path).read_bytes()
    ext = Path(logo_path).suffix.lstrip(".").lower()
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext or "png"
    return f"data:image/{mime};base64,{base64.b64encode(data).decode('ascii')}"


def build_digest_context(
    conn: sqlite3.Connection, config: dict, digest_week: str, week_start: date, week_end: date
) -> dict[str, Any]:
    deals = db.get_deals_for_week(conn, digest_week)
    digest_cfg = config.get("digest", {})
    top_deals_count = digest_cfg.get("top_deals_count", 5)
    min_pe_vc_size = digest_cfg.get("pe_vc_min_size_usd_m", 0)
    sections = group_deals(deals, top_deals_count, min_pe_vc_size)
    return {
        "week_start": week_start,
        "week_end": week_end,
        "sections": sections,
        "total_deals": len(deals),
        "logo_data_uri": _load_logo_data_uri(digest_cfg.get("logo_path")),
        "site_url": digest_cfg.get("site_url", "#"),
        "brand": digest_cfg.get("brand", {"navy": "#0b2545", "orange": "#F89A55", "gray": "#6C7A89"}),
    }


def render_digest(context: dict[str, Any], template_path: str) -> str:
    template_file = Path(template_path)
    env = Environment(
        loader=FileSystemLoader(str(template_file.parent)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(template_file.name)
    return template.render(**context)


def build_and_render(
    conn: sqlite3.Connection, config: dict, digest_week: str, week_start: date, week_end: date
) -> str:
    context = build_digest_context(conn, config, digest_week, week_start, week_end)
    template_path = config.get("digest", {}).get("template_path", "digest/template.html")
    return render_digest(context, template_path)
