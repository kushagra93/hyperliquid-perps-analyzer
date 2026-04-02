# main.py
# ─────────────────────────────────────────────────────────────────
# Entry point for the NVDA futures flow.
# Runs a cron loop every CRON_INTERVAL_SECONDS.
# On each tick:
#   1. Price monitor checks for threshold breach
#   2. If breached: OI tracker snapshots current OI
#   3. Agent 1 fetches news, Agent 2 builds OI report (parallel)
#   4. Condition engine classifies C1–C4
#   5. Causality rules decide whether to alert
#   6. Agent 3 runs LLM causality analysis
#   7. Notifier logs to Google Sheets
# ─────────────────────────────────────────────────────────────────

import time
import logging
import schedule
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Imports ───────────────────────────────────────────────────────
from config.settings import CRON_INTERVAL_SECONDS, ASSET
from core.price_monitor import PriceMonitor
from core.oi_tracker import OITracker
from core.condition_engine import evaluate_condition, should_alert
from agents.agent1_news import fetch_news
from agents.agent2_oi import build_oi_report
from agents.agent3_causality import run_causality_analysis
from notifiers.sheets import log_alert

# ── Shared state ──────────────────────────────────────────────────
price_monitor = PriceMonitor()
oi_tracker = OITracker()

# Cooldown: prevent re-firing alerts within N seconds of the last one
ALERT_COOLDOWN_SECONDS = 300   # 5 minutes
_last_alert_time = 0


def run_tick():
    """Called every CRON_INTERVAL_SECONDS."""
    global _last_alert_time

    logger.info(f"── Tick ── {ASSET} ──────────────────────────────────────────")

    # ── Step 1: OI tick (always running to build history) ─────────
    oi_snapshot = oi_tracker.tick()

    # ── Step 2: Price tick ────────────────────────────────────────
    price_trigger = price_monitor.tick()

    if price_trigger is None:
        return   # No threshold breach — nothing to do

    logger.info(f"[Main] Price threshold breached: {price_trigger['price_change_pct']:+.2f}%")

    # ── Cooldown check ────────────────────────────────────────────
    now = time.time()
    if now - _last_alert_time < ALERT_COOLDOWN_SECONDS:
        remaining = int(ALERT_COOLDOWN_SECONDS - (now - _last_alert_time))
        logger.info(f"[Main] Cooldown active — {remaining}s remaining. Skipping.")
        return

    if oi_snapshot is None:
        logger.warning("[Main] OI snapshot unavailable. Skipping agent pipeline.")
        return

    # ── Step 3: Condition classification ─────────────────────────
    condition = evaluate_condition(price_trigger, oi_snapshot)
    if condition is None:
        logger.info("[Main] No condition matched. Skipping.")
        return

    # ── Step 4: Run Agent 1 + Agent 2 in parallel ─────────────────
    logger.info("[Main] Firing Agent 1 (news) and Agent 2 (OI) in parallel...")
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_news = executor.submit(fetch_news)
        future_oi = executor.submit(build_oi_report, oi_snapshot)

        news_report = future_news.result()
        oi_report = future_oi.result()

    logger.info(
        f"[Main] Agent 1 done — has_news={news_report['has_news']}, "
        f"{len(news_report['articles'])} articles"
    )
    logger.info(f"[Main] Agent 2 done — {oi_report['interpretation'][:80]}...")

    # ── Step 5: Causality rules — should we alert? ─────────────────
    if not should_alert(condition, news_report):
        logger.info("[Main] Causality rules: alert suppressed.")
        return

    # ── Step 6: Agent 3 — LLM causality analysis ──────────────────
    logger.info("[Main] Firing Agent 3 (causality LLM)...")
    causality = run_causality_analysis(price_trigger, news_report, oi_report, condition)
    logger.info(
        f"[Main] Agent 3 done — verdict: {causality.get('verdict', '')[:80]}"
    )

    # ── Step 7: Log to Google Sheets ──────────────────────────────
    log_alert(price_trigger, oi_report, condition, causality, news_report)
    _last_alert_time = time.time()

    logger.info(
        f"[Main] ✓ Alert logged — {condition['condition_id']} | "
        f"{causality.get('confidence','?')} confidence | "
        f"{causality.get('primary_driver','?')} driver"
    )


def main():
    logger.info("=" * 60)
    logger.info(f"  NVDA Flow starting — monitoring {ASSET} on Hyperliquid")
    logger.info(f"  Cron interval : {CRON_INTERVAL_SECONDS}s")
    logger.info(f"  Alert cooldown: {ALERT_COOLDOWN_SECONDS}s")
    logger.info("=" * 60)

    # Run once immediately on start, then on schedule
    run_tick()
    schedule.every(CRON_INTERVAL_SECONDS).seconds.do(run_tick)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
