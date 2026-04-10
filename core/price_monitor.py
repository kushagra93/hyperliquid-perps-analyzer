# core/price_monitor.py
# ─────────────────────────────────────────────────────────────────
# Polls Hyperliquid mark price for the configured asset every tick.
# Maintains a rolling price history and fires when the configured
# % threshold is breached within the rolling window.
# ─────────────────────────────────────────────────────────────────

import requests
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from config.settings import (
    ASSET,
    HL_PERP_DEX,
    PRICE_CHANGE_THRESHOLD_PCT,
    PRICE_WINDOW_MINUTES,
)
from core.hl_client import fetch_meta_and_asset_ctxs
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_mark_price(asset: str) -> float | None:
    """
    Fetch the current mark price for an asset from Hyperliquid.
    Uses the public unauthenticated metaAndAssetCtxs endpoint.
    """
    try:
        data = fetch_meta_and_asset_ctxs()
        if not data:
            return None

        # data is [meta, assetCtxs]
        # meta["universe"] is a list of asset dicts with "name"
        # assetCtxs is a parallel list of market context dicts
        meta = data[0]
        asset_ctxs = data[1]

        expected_names = {asset.upper()}
        if HL_PERP_DEX:
            expected_names.add(f"{HL_PERP_DEX}:{asset}".upper())

        for i, info in enumerate(meta["universe"]):
            if info["name"].upper() in expected_names:
                mark_px = float(asset_ctxs[i]["markPx"])
                return mark_px

        logger.warning(f"Asset '{asset}' not found in HL universe (dex={HL_PERP_DEX or 'default'}).")
        return None

    except Exception as e:
        logger.error(f"Error fetching mark price: {e}")
        return None


class PriceMonitor:
    """
    Maintains a rolling deque of (timestamp, price) tuples.
    On each tick, checks whether the oldest price in the window
    differs from the current price by >= threshold %.

    Returns a trigger dict if breached, else None.
    """

    def __init__(self):
        self.window_seconds = PRICE_WINDOW_MINUTES * 60
        self.threshold_pct = PRICE_CHANGE_THRESHOLD_PCT
        # Store (datetime, price) tuples
        self.history: deque = deque()

    def _prune_old(self):
        cutoff = datetime.now(IST) - timedelta(seconds=self.window_seconds)
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def tick(self) -> dict | None:
        """
        Call this on every cron tick.
        Returns a trigger dict if threshold breached, else None.

        Trigger dict:
        {
            "asset": str,
            "current_price": float,
            "window_start_price": float,
            "price_change_pct": float,   # negative = down
            "triggered_at": datetime,
        }
        """
        price = fetch_mark_price(ASSET)
        if price is None:
            return None

        now = datetime.now(IST)
        self.history.append((now, price))
        self._prune_old()

        if len(self.history) < 2:
            logger.debug(f"[PriceMonitor] Tick — price={price}, history too short to evaluate.")
            return None

        window_start_price = self.history[0][1]
        change_pct = ((price - window_start_price) / window_start_price) * 100

        logger.info(
            f"[PriceMonitor] {ASSET} price={price:.4f} | "
            f"window_start={window_start_price:.4f} | Δ={change_pct:+.2f}%"
        )

        if abs(change_pct) >= self.threshold_pct:
            logger.info(f"[PriceMonitor] THRESHOLD BREACHED — Δ={change_pct:+.2f}%")
            return {
                "asset": ASSET,
                "current_price": price,
                "window_start_price": window_start_price,
                "price_change_pct": change_pct,
                "triggered_at": now,
            }

        return None
