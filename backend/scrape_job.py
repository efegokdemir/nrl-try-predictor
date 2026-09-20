"""
Standalone scraper job, run by the manual .github/workflows/scrape.yml dispatch.
Syncs current season data and backfills any new matches.
"""

import os
import sys
import logging
import asyncio

# Ensure we can import sibling modules
sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_total_match_count, get_total_try_count, prune_old_logs
from scraper import scrape_all, sync_current_season
import log_handler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    init_db()

    # Mirror logs into Supabase (app_logs table) so scrape runs are visible
    # alongside the backend's logs, tagged source="scraper".
    log_handler.attach(source="scraper")
    try:
        prune_old_logs()
    except Exception as e:
        logger.warning(f"Failed to prune old app_logs rows: {e}")

    count = get_total_match_count()
    logger.info(f"DB has {count} matches.")

    if count == 0:
        # Fresh database — run full historical scrape
        logger.info("Empty database detected. Running full historical scrape...")
        asyncio.run(scrape_all())
    else:
        # Existing data — just sync recent rounds
        logger.info("Running current season sync...")
        asyncio.run(sync_current_season())

    final = get_total_match_count()
    tries = get_total_try_count()
    logger.info(f"Done. DB now has {final} matches, {tries} tries.")


if __name__ == "__main__":
    main()
