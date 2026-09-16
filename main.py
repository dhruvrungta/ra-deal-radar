"""Weekly entrypoint: ingest -> classify -> dedupe -> build digest -> send.

Run via cron or a GitHub Actions scheduled workflow, e.g.:
    python main.py
    python main.py --no-send   # build the digest and write it to disk, skip email
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import yaml

from digest.build_digest import build_and_render
from ingest.fetch import ingest_and_store
from process.classify import classify_articles
from process.dedupe import dedupe_and_store
from send.mailer import send_digest
from store import db

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("main")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def week_bounds(today: date) -> tuple[date, date, str]:
    """Monday-start week containing `today`. digest_week is the Monday's
    ISO date, used as the key tying deals to the digest run they appeared in."""
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start, week_end, week_start.isoformat()


def run(no_send: bool = False, run_date: date | None = None) -> None:
    config = load_config()
    db_path = config.get("database", {}).get("path", "store/deal_radar.db")
    db.init_db(db_path)

    today = run_date or date.today()
    week_start, week_end, digest_week = week_bounds(today)
    logger.info("Running RA Deal Radar for week of %s", week_start)

    with db.connect(db_path) as conn:
        new_article_count = ingest_and_store(config, conn)
        logger.info("%d new articles ingested", new_article_count)

        unclassified = db.get_unclassified_articles(conn)
        logger.info("%d articles pending classification", len(unclassified))

        deals, processed_article_ids = classify_articles(unclassified, config)
        logger.info("%d articles classified as deals", len(deals))

        # Only mark articles whose batch actually got a usable API response;
        # ones whose batch errored out stay unclassified and retry next run.
        db.mark_articles_classified(conn, processed_article_ids)

        final_ids = dedupe_and_store(conn, deals, config, digest_week)
        logger.info("%d unique deals stored/updated for this run", len(final_ids))

        html = build_and_render(conn, config, digest_week, week_start, week_end)

    output_path = Path(config.get("digest", {}).get("output_path", "output/digest_latest.html"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Digest written to %s", output_path)

    if no_send:
        logger.info("--no-send passed, skipping email delivery")
        return

    send_digest(html, config, week_start)


def main() -> None:
    parser = argparse.ArgumentParser(description="RA Deal Radar weekly digest")
    parser.add_argument(
        "--no-send", action="store_true", help="Build the digest but don't email it"
    )
    args = parser.parse_args()
    run(no_send=args.no_send)


if __name__ == "__main__":
    main()
