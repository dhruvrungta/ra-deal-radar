"""Classifies raw articles into structured Deal records via a batched Gemini call.

Uses the Gemini API (a Google AI Studio key works — Flash/Flash-Lite models
are free-tier, no credit card required) with structured JSON output rather
than Claude, since the free tier avoids per-token LLM spend for this workload.
Swap the client/model here if you'd rather pay for Claude/GPT accuracy later;
nothing else in the pipeline depends on which provider does the classifying.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from google import genai
from google.genai import errors, types

from process.schema import Deal, RawArticle

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are analyzing news articles for a boutique M&A/PE advisory \
firm's weekly deal-tracking digest covering India-related transactions.

For EACH article given, decide whether it clearly describes a specific M&A, \
private equity, or venture capital transaction (a deal involving an acquirer \
and/or target company, an investment round, a buyout, or a formal exit). \
If it does NOT — e.g. general market commentary, opinion pieces, unrelated \
corporate news, earnings reports with no deal, or a deal too vague to extract \
any party names — set "is_deal" to false and leave the other fields empty.

Set "index" on every result to the article's bracketed number (e.g. the
article marked "[3]" gets index 3) — this is how results get matched back to
articles, so it must be correct even if you skip or reorder articles.

If it DOES describe a deal, set "is_deal" to true and extract:
- acquirer / target: company names as stated (best guess if only one side named)
- sector: the target's industry, in a few words
- deal_size_usd_m: deal value in USD millions if stated or reasonably inferable
  from a stated INR/USD figure; omit if not disclosed
- deal_type: one of ma_strategic, pe_buyout, pe_growth, pe_minority, vc, pe_exit
- cross_border: "domestic" if both acquirer and target are India-based,
  "inbound" if a foreign acquirer is buying an India target, "outbound" if an
  India acquirer is buying a foreign target
- stage: one of rumored, announced, definitive_agreement, completed, terminated
  — based on the language used (e.g. "in talks" = rumored, "signs definitive
  agreement" = definitive_agreement, "completes acquisition" = completed)
- advisors: list of legal/financial advisor firm names EXPLICITLY mentioned as
  advising a party on this transaction (e.g. "XYZ Law advised the acquirer").
  Empty list if none mentioned — do not guess.
- summary: a single, information-dense 1-2 sentence brief of the deal

Return exactly one result per article, matching the response schema."""

RESULT_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "index": {"type": "INTEGER"},
        "is_deal": {"type": "BOOLEAN"},
        "acquirer": {"type": "STRING", "nullable": True},
        "target": {"type": "STRING", "nullable": True},
        "sector": {"type": "STRING", "nullable": True},
        "deal_size_usd_m": {"type": "NUMBER", "nullable": True},
        "deal_type": {
            "type": "STRING",
            "enum": [
                "ma_strategic",
                "pe_buyout",
                "pe_growth",
                "pe_minority",
                "vc",
                "pe_exit",
            ],
            "nullable": True,
        },
        "cross_border": {
            "type": "STRING",
            "enum": ["domestic", "inbound", "outbound"],
            "nullable": True,
        },
        "stage": {
            "type": "STRING",
            "enum": [
                "rumored",
                "announced",
                "definitive_agreement",
                "completed",
                "terminated",
            ],
            "nullable": True,
        },
        "advisors": {"type": "ARRAY", "items": {"type": "STRING"}},
        "summary": {"type": "STRING", "nullable": True},
    },
    "required": ["index", "is_deal"],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {"type": "ARRAY", "items": RESULT_ITEM_SCHEMA},
    },
    "required": ["results"],
}


def _build_batch_prompt(articles: list[RawArticle]) -> str:
    lines = []
    for i, a in enumerate(articles):
        text = (a.raw_text or "").strip().replace("\n", " ")
        if len(text) > 800:
            text = text[:800] + "..."
        lines.append(
            f"[{i}] source={a.source} published={a.published_date}\n"
            f"headline: {a.headline}\n"
            f"text: {text}"
        )
    return (
        f"Classify these {len(articles)} articles. Return exactly {len(articles)} "
        "results in order.\n\n" + "\n\n".join(lines)
    )


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


RETRYABLE_STATUS_CODES = {429, 503}
MAX_RETRY_WAIT_SECONDS = 65


def _server_retry_delay(exc: errors.APIError) -> Optional[float]:
    """429 responses tell us exactly how long to wait (RetryInfo.retryDelay,
    e.g. "52s") — parsing that beats guessing with exponential backoff,
    especially for a per-minute or per-day quota reset."""
    details = getattr(exc, "details", None)
    if not isinstance(details, list):
        return None
    for item in details:
        if isinstance(item, dict) and item.get("retryDelay"):
            match = re.match(r"([\d.]+)s?$", str(item["retryDelay"]))
            if match:
                return float(match.group(1))
    return None


def _generate_with_retry(
    client: genai.Client, model: str, contents: str, config: types.GenerateContentConfig,
    max_attempts: int = 3,
) -> types.GenerateContentResponse:
    """The free tier gets 503s under load fairly often, and this project's
    request volume can bump into the per-minute rate limit too — a
    backoff-retry avoids losing a whole batch (and delaying it a full week)
    to either."""
    last_exc: Optional[errors.APIError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as exc:
            last_exc = exc
            if getattr(exc, "code", None) not in RETRYABLE_STATUS_CODES or attempt == max_attempts:
                raise
            wait = _server_retry_delay(exc) or (2**attempt)
            wait = min(wait, MAX_RETRY_WAIT_SECONDS)
            logger.warning(
                "Gemini call failed (%s), retrying in %.0fs (attempt %d/%d)",
                exc, wait, attempt, max_attempts,
            )
            time.sleep(wait)
    raise last_exc  # unreachable, satisfies type checkers


def classify_articles(
    articles: list[RawArticle],
    config: dict,
    client: Optional[genai.Client] = None,
) -> tuple[list[Deal], list[str]]:
    """Classifies a list of raw articles. Returns (deals, processed_article_ids):
    `deals` are the ones judged to be real deals, as validated Deal objects;
    `processed_article_ids` are every article whose batch got a usable API
    response, whether or not it turned out to be a deal — the caller should
    mark only these as classified. Articles in a batch that failed outright
    (API error, unparseable response) are left off `processed_article_ids` so
    they get retried on the next run."""
    if not articles:
        return [], []

    client = client or genai.Client(
        api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    cfg = config.get("classification", {})
    model = cfg.get("model", "gemini-2.5-flash")
    batch_size = cfg.get("batch_size", 25)
    max_tokens = cfg.get("max_tokens", 8000)
    # The free tier's per-minute rate limit (observed as low as 5 req/min on
    # gemini-3.6-flash) is tighter than its per-day cap — pacing calls to
    # stay under it avoids most of the 429s a large multi-batch run would
    # otherwise hit, rather than relying on reactive retries for each one.
    min_seconds_between_calls = cfg.get("min_seconds_between_calls", 13)

    deals: list[Deal] = []
    processed_article_ids: list[str] = []
    last_call_at: Optional[float] = None

    for batch in _chunk(articles, batch_size):
        if last_call_at is not None:
            elapsed = time.monotonic() - last_call_at
            if elapsed < min_seconds_between_calls:
                time.sleep(min_seconds_between_calls - elapsed)
        last_call_at = time.monotonic()
        try:
            response = _generate_with_retry(
                client,
                model,
                _build_batch_prompt(batch),
                types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    max_output_tokens=max_tokens,
                    # Deterministic extraction doesn't need reasoning, and
                    # thinking tokens eat into max_output_tokens on Gemini 3.x
                    # models, which was truncating the JSON mid-response for
                    # full-size batches.
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        except errors.APIError as exc:
            logger.error("Classification API call failed for batch: %s", exc)
            continue

        try:
            parsed = json.loads(response.text)
            results = parsed.get("results", [])
        except (ValueError, AttributeError, TypeError) as exc:
            logger.error("Could not parse classification response as JSON: %s", exc)
            continue

        # Match results back to articles by the "index" field rather than
        # position: if the model returns fewer results than articles, a
        # positional zip would silently shift every later article onto the
        # wrong classification (wrong headline paired with wrong summary/
        # acquirer/target). Index-based lookup only loses the specific
        # article that's actually missing.
        results_by_index: dict[int, dict] = {}
        for r in results:
            idx = r.get("index")
            if isinstance(idx, int) and 0 <= idx < len(batch):
                results_by_index[idx] = r
            else:
                logger.warning("Dropping result with invalid index: %r", idx)

        if len(results_by_index) != len(batch):
            missing = set(range(len(batch))) - results_by_index.keys()
            logger.warning(
                "Classification returned %d/%d usable results; missing articles: %s",
                len(results_by_index),
                len(batch),
                [batch[i].url for i in missing],
            )

        processed_article_ids.extend(
            batch[i].article_id for i in results_by_index
        )

        for i, article in enumerate(batch):
            result = results_by_index.get(i)
            if result is None:
                continue
            if not result.get("is_deal"):
                continue
            if not article.published_date:
                logger.warning(
                    "Skipping deal from %s: article has no published_date", article.url
                )
                continue
            # The model occasionally marks is_deal=true but leaves a required
            # enum blank when it's unsure. Defaulting keeps the deal in the
            # digest (flagged as uncertain by the fallback value) rather than
            # silently losing real coverage to a pydantic validation error.
            if not result.get("cross_border"):
                logger.warning(
                    "%s: missing cross_border, defaulting to domestic", article.url
                )
                result["cross_border"] = "domestic"
            if not result.get("deal_type"):
                logger.warning(
                    "%s: missing deal_type, defaulting to ma_strategic", article.url
                )
                result["deal_type"] = "ma_strategic"
            if not result.get("stage"):
                logger.warning("%s: missing stage, defaulting to announced", article.url)
                result["stage"] = "announced"
            try:
                deal = Deal(
                    id=Deal.compute_id(
                        result.get("acquirer"),
                        result.get("target"),
                        article.published_date,
                        headline_fallback=article.headline,
                    ),
                    article_id=article.article_id,
                    headline=article.headline,
                    source=article.source,
                    url=article.url,
                    published_date=article.published_date,
                    acquirer=result.get("acquirer"),
                    target=result.get("target"),
                    sector=result.get("sector"),
                    deal_size_usd_m=result.get("deal_size_usd_m"),
                    deal_type=result.get("deal_type"),
                    cross_border=result.get("cross_border"),
                    stage=result.get("stage"),
                    advisors=result.get("advisors") or [],
                    summary=result.get("summary") or article.headline,
                )
            except Exception as exc:
                logger.warning("Dropping malformed classification for %s: %s", article.url, exc)
                continue
            deals.append(deal)

    return deals, processed_article_ids
