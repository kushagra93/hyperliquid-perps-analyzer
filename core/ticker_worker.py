# core/ticker_worker.py
# ─────────────────────────────────────────────────────────────────
# Self-contained worker for a single ticker.
# Uses the existing hl_client session (with retry logic) for all
# HL API calls — no duplicate session setup here.
#
# One instance per ticker. run_tick() is called concurrently
# by main.py's ThreadPoolExecutor on every cron interval.
# Each worker owns its own history deques and cooldown timer
# so tickers are fully isolated from each other.
# ─────────────────────────────────────────────────────────────────

import time
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from core.hl_client import fetch_meta_and_asset_ctxs
from core.condition_engine import evaluate_condition, should_alert
from agents.agent1_news import fetch_news
from agents.agent2_oi import build_oi_report_for_ticker
from agents.agent3_causality import run_causality_analysis
from notifiers.sheets import log_alert

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _extract_ctx(data: list, hl_asset: str) -> dict | None:
    """Extract a single asset's ctx from the full metaAndAssetCtxs response."""
    if not data:
        return None
    universe = data[0].get("universe", [])
    ctxs = data[1]
    for i, asset in enumerate(universe):
        if asset.get("name") == hl_asset and i < len(ctxs):
            return ctxs[i]
    return None


class TickerWorker:
    """
    Owns all state for one ticker. Public method: run_tick().
    """

    def __init__(self, symbol: str, cfg: dict):
        self.symbol = symbol
        self.cfg = cfg
        self.hl_asset = cfg["hl_asset"]
        self.full_name = cfg.get("full_name", symbol)

        # Thresholds from per-ticker config
        self.price_threshold_pct = cfg["price_change_threshold_pct"]
        self.price_window_sec = cfg["price_window_minutes"] * 60
        self.vol_threshold_pct = cfg["volume_change_threshold_pct"]
        self.vol_window_sec = cfg["volume_window_minutes"] * 60
        self.vol_reset_drop_pct = cfg.get("volume_reset_drop_pct", 30.0)
        self.oi_window_sec = cfg["oi_window_hours"] * 3600
        self.enable_volume_trigger = cfg.get("enable_volume_trigger", True)
        self.cooldown_sec = cfg.get("alert_cooldown_seconds", 300)
        self.sheets_tab = cfg.get("sheets_tab", symbol)

        # Rolling histories — (timestamp, value) or (timestamp, value, delta)
        self.price_history: deque = deque()
        self.vol_history: deque = deque()
        self.oi_history: deque = deque()

        # Volume reset detection state
        self._last_volume: float | None = None

        # Per-ticker cooldown
        self._last_alert_time: float = 0

    # ── History pruning ───────────────────────────────────────────

    def _prune(self):
        now = datetime.now(IST)
        for history, window_sec in [
            (self.price_history, self.price_window_sec),
            (self.vol_history, self.vol_window_sec),
            (self.oi_history, self.oi_window_sec),
        ]:
            cutoff = now - timedelta(seconds=window_sec)
            while history and history[0][0] < cutoff:
                history.popleft()

    # ── Price ─────────────────────────────────────────────────────

    def _check_price(self, price: float, now: datetime) -> dict | None:
        self.price_history.append((now, price))
        if len(self.price_history) < 2:
            return None
        window_start = self.price_history[0][1]
        change_pct = ((price - window_start) / window_start) * 100
        logger.info(f"[{self.symbol}] price={price:.4f} Δ={change_pct:+.2f}%")
        if abs(change_pct) >= self.price_threshold_pct:
            logger.info(f"[{self.symbol}] PRICE BREACH {change_pct:+.2f}%")
            return {
                "asset": self.symbol,
                "current_price": price,
                "window_start_price": window_start,
                "price_change_pct": change_pct,
                "triggered_at": now,
                "trigger_source": "price",
                "volume_trigger": None,
            }
        return None

    # ── Volume ────────────────────────────────────────────────────

    def _check_volume(self, volume: float, now: datetime) -> dict | None:
        if not self.enable_volume_trigger:
            return None

        tick_delta = 0.0
        if self._last_volume is not None:
            prev = self._last_volume
            drop_pct = ((prev - volume) / prev * 100) if prev > 0 else 0.0
            if drop_pct >= self.vol_reset_drop_pct:
                logger.info(f"[{self.symbol}] Daily volume reset ({prev:.0f}→{volume:.0f}). Skipping delta.")
            else:
                tick_delta = max(volume - prev, 0.0)
        self._last_volume = volume

        self.vol_history.append((now, volume, tick_delta))
        if len(self.vol_history) < 2:
            return None

        window_start_vol = self.vol_history[0][1]
        window_delta = sum(d for _, _, d in list(self.vol_history)[1:])
        if window_start_vol <= 0:
            return None

        change_pct = (window_delta / window_start_vol) * 100
        logger.info(f"[{self.symbol}] vol_delta={window_delta:.0f} Δ={change_pct:+.2f}%")

        if change_pct >= self.vol_threshold_pct:
            logger.info(f"[{self.symbol}] VOLUME BREACH {change_pct:+.2f}%")
            return {
                "asset": self.symbol,
                "current_volume": volume,
                "window_start_volume": window_start_vol,
                "volume_change_pct": change_pct,
                "window_delta": window_delta,
                "triggered_at": now,
            }
        return None

    # ── OI ────────────────────────────────────────────────────────

    def _update_oi(self, oi: float, now: datetime) -> dict:
        self.oi_history.append((now, oi))
        if len(self.oi_history) < 2:
            return {"current_oi": oi, "baseline_oi": oi, "oi_change_pct": 0.0, "direction": "flat"}
        baseline = self.oi_history[0][1]
        change_pct = ((oi - baseline) / baseline * 100) if baseline else 0.0
        direction = "up" if change_pct > 0.5 else "down" if change_pct < -0.5 else "flat"
        logger.info(f"[{self.symbol}] OI={oi:.2f} Δ={change_pct:+.2f}% dir={direction}")
        return {"current_oi": oi, "baseline_oi": baseline, "oi_change_pct": change_pct, "direction": direction}

    # ── Main tick ─────────────────────────────────────────────────

    def run_tick(self, data: list | None = None):
        # Reuse shared data passed from main loop when available.
        # Fallback to direct fetch for backward compatibility.
        if data is None:
            data = fetch_meta_and_asset_ctxs()
        ctx = _extract_ctx(data, self.hl_asset)
        if ctx is None:
            logger.warning(f"[{self.symbol}] Not found in HL universe. Skipping.")
            return

        price = float(ctx.get("markPx") or ctx.get("midPx") or 0)
        volume = float(ctx.get("dayNtlVlm") or 0)
        oi = float(ctx.get("openInterest") or 0)

        if not price:
            return

        now = datetime.now(IST)
        self._prune()

        # Always update OI history regardless of trigger
        oi_snapshot = self._update_oi(oi, now)

        # Check triggers
        price_trigger = self._check_price(price, now)
        volume_trigger = self._check_volume(volume, now)

        # Trigger gate — either must fire
        if price_trigger is None and volume_trigger is None:
            return

        # Cooldown check
        if time.time() - self._last_alert_time < self.cooldown_sec:
            remaining = int(self.cooldown_sec - (time.time() - self._last_alert_time))
            logger.info(f"[{self.symbol}] Cooldown — {remaining}s remaining.")
            return

        # Build unified trigger context
        if price_trigger is None:
            # Volume-only: synthesize flat price trigger, use volume dir as fallback
            effective_trigger = {
                "asset": self.symbol,
                "current_price": price,
                "window_start_price": price,
                "price_change_pct": 0.0,
                "triggered_at": now,
                "trigger_source": "volume",
                "volume_trigger": volume_trigger,
            }
        else:
            effective_trigger = price_trigger
            effective_trigger["trigger_source"] = "price+volume" if volume_trigger else "price"
            effective_trigger["volume_trigger"] = volume_trigger

        # Condition classification
        condition = evaluate_condition(effective_trigger, oi_snapshot)
        if condition is None:
            logger.info(f"[{self.symbol}] No condition matched.")
            return

        # Agent 1 (news) + Agent 2 (OI report) in parallel
        # Agent 2 reuses ctx already fetched — no extra API call
        logger.info(f"[{self.symbol}] Firing agents...")
        with ThreadPoolExecutor(max_workers=2) as ex:
            future_news = ex.submit(fetch_news, self.symbol, self.full_name)
            future_oi = ex.submit(build_oi_report_for_ticker, oi_snapshot, volume_trigger, ctx)
            news_report = future_news.result()
            oi_report = future_oi.result()

        # Alert gate
        if not should_alert(condition, news_report):
            logger.info(f"[{self.symbol}] Alert suppressed.")
            return

        # Agent 3 — causality LLM
        causality = run_causality_analysis(
            effective_trigger, news_report, oi_report, condition
        )
        logger.info(f"[{self.symbol}] Verdict: {causality.get('verdict','')[:80]}")

        # Log to per-ticker Sheets tab
        log_alert(
            price_trigger=effective_trigger,
            oi_report=oi_report,
            condition=condition,
            causality=causality,
            news_report=news_report,
            sheets_tab=self.sheets_tab,
        )
        self._last_alert_time = time.time()

        logger.info(
            f"[{self.symbol}] Alert logged — {condition['condition_id']} | "
            f"{causality.get('confidence','?')} confidence | "
            f"source={effective_trigger['trigger_source']}"
        )
