# main.py
# ─────────────────────────────────────────────────────────────────
# Multi-ticker entry point.
# Spawns one TickerWorker per ticker defined in config/tickers.py.
# On each cron tick, all workers run concurrently via ThreadPoolExecutor.
# Each worker is fully isolated — own state, own cooldown, own Sheet tab.
#
# The existing single-ticker flow (price_monitor, volume_monitor,
# oi_tracker) is replaced by ticker_worker which consolidates all
# three into one class per ticker.
# ─────────────────────────────────────────────────────────────────

import time
import logging
import schedule
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

from config.settings import CRON_INTERVAL_SECONDS, validate_runtime_settings
from config.tickers import TICKERS
from core.hl_client import fetch_meta_and_asset_ctxs
from core.ticker_worker import TickerWorker

# Instantiate one worker per ticker at startup
workers: list[TickerWorker] = [
    TickerWorker(symbol=symbol, cfg=cfg)
    for symbol, cfg in TICKERS.items()
]


def run_all_tickers():
    """
    Called every CRON_INTERVAL_SECONDS.
    All ticker workers run concurrently — each makes one HL API call.
    Errors in one ticker don't affect others.
    """
    logger.info(f"── Cron tick — {len(workers)} tickers ────────────────────────")

    # Fetch once per cron tick and share across workers.
    # This avoids N concurrent duplicate HL requests.
    shared_data = fetch_meta_and_asset_ctxs()
    if shared_data is None:
        logger.warning("[Main] HL data unavailable for this tick. Skipping all workers.")
        return

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        futures = {executor.submit(w.run_tick, shared_data): w.symbol for w in workers}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error(f"[{symbol}] Unhandled worker error: {e}")


def main():
    validate_runtime_settings()

    logger.info("=" * 60)
    logger.info(f"  Multi-ticker HL analyzer starting")
    logger.info(f"  Tickers ({len(workers)}): {[w.symbol for w in workers]}")
    logger.info(f"  Cron interval: {CRON_INTERVAL_SECONDS}s")
    logger.info("=" * 60)

    run_all_tickers()
    schedule.every(CRON_INTERVAL_SECONDS).seconds.do(run_all_tickers)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
