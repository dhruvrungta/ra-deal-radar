"""Pydantic models shared across ingest, classification, storage, and digest."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

DealType = Literal[
    "ma_strategic",
    "pe_buyout",
    "pe_growth",
    "pe_minority",
    "vc",
    "pe_exit",
]

CrossBorder = Literal["domestic", "inbound", "outbound"]

Stage = Literal[
    "rumored",
    "announced",
    "definitive_agreement",
    "completed",
    "terminated",
]


def make_article_id(source: str, url: str) -> str:
    """Stable id for a raw article, independent of its eventual classification."""
    return hashlib.sha256(f"{source}|{url}".encode("utf-8")).hexdigest()[:24]


def normalize_for_hash(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class RawArticle(BaseModel):
    """An unclassified article pulled straight from a source."""

    article_id: str
    source: str
    headline: str
    url: str
    published_date: Optional[date] = None
    raw_text: str = ""

    @classmethod
    def create(
        cls,
        source: str,
        headline: str,
        url: str,
        published_date: Optional[date],
        raw_text: str = "",
    ) -> "RawArticle":
        return cls(
            article_id=make_article_id(source, url),
            source=source,
            headline=headline,
            url=url,
            published_date=published_date,
            raw_text=raw_text,
        )


class Deal(BaseModel):
    id: str  # hash of normalized (acquirer, target, published_date) for dedup
    article_id: str  # links back to the RawArticle it was classified from
    headline: str
    source: str
    url: str
    published_date: date
    acquirer: Optional[str] = None
    target: Optional[str] = None
    sector: Optional[str] = None
    deal_size_usd_m: Optional[float] = None
    deal_type: DealType
    cross_border: CrossBorder
    stage: Stage
    advisors: list[str] = Field(default_factory=list)
    summary: str

    @field_validator("deal_size_usd_m")
    @classmethod
    def _non_negative(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("deal_size_usd_m must be non-negative")
        return v

    @classmethod
    def compute_id(
        cls,
        acquirer: Optional[str],
        target: Optional[str],
        published_date: date,
        headline_fallback: str = "",
    ) -> str:
        key = normalize_for_hash(acquirer) + "|" + normalize_for_hash(target)
        if not key.strip("|"):
            # No parties extracted — fall back to headline+date so we don't
            # collapse unrelated no-party deals published the same day.
            key = f"unknown|{published_date.isoformat()}|{normalize_for_hash(headline_fallback)}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
