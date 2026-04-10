# core/oi_tracker.py
# ─────────────────────────────────────────────────────────────────
# Fetches open interest from Hyperliquid on every tick.
# Maintains a 3-hour rolling baseline and computes OI delta
# to classify directional OI movement.
# ─────────────────────────────────────────────────────────────────

import requests
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from config.settings import ASSET, HL_PERP_DEX, OI_WINDOW_HOURS
from core.hl_client import fetch_meta_and_asset_ctxs
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_open_interest(asset: str) -> float | None:
    """
    Fetch current open interest for an asset from Hyperliquid.
    OI is returned in the asset's native units.
    """
    try:
        data = fetch_meta_and_asset_ctxs()
        if not data:
            return None

        meta = data[0]
        asset_ctxs = data[1]

        expected_names = {asset.upper()}
        if HL_PERP_DEX:
            expected_names.add(f"{HL_PERP_DEX}:{asset}".upper())

        for i, info in enumerate(meta["universe"]):
            if info["name"].upper() in expected_names:
                oi = float(asset_ctxs[i]["openInterest"])
                return oi

        logger.warning(f"Asset '{asset}' not found for OI fetch (dex={HL_PERP_DEX or 'default'}).")
        return None

    except Exception as e:
        logger.error(f"Error fetching open interest: {e}")
        return None


class OITracker:
    """
    Maintains a rolling deque of (timestamp, oi) tuples over OI_WINDOW_HOURS.
    On tick(), returns an OI snapshot dict with current OI,
    baseline OI (oldest in window), and % change.
    """

    def __init__(self):
        self.window_seconds = OI_WINDOW_HOURS * 3600
        self.history: deque = deque()

    def _prune_old(self):
        cutoff = datetime.now(IST) - timedelta(seconds=self.window_seconds)
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def tick(self) -> dict | None:
        """
        Returns OI snapshot dict:
        {
            "current_oi": float,
            "baseline_oi": float,
            "oi_change_pct": float,   # negative = OI falling
            "direction": "up" | "down" | "flat",
        }
        """
        oi = fetch_open_interest(ASSET)
        if oi is None:
            return None

        now = datetime.now(IST)
        self.history.append((now, oi))
        self._prune_old()

        if len(self.history) < 2:
            return {
                "current_oi": oi,
                "baseline_oi": oi,
                "oi_change_pct": 0.0,
                "direction": "flat",
            }

        baseline_oi = self.history[0][1]
        change_pct = ((oi - baseline_oi) / baseline_oi) * 100 if baseline_oi else 0.0

        if change_pct > 0.5:
            direction = "up"
        elif change_pct < -0.5:
            direction = "down"
        else:
            direction = "flat"

        logger.info(
            f"[OITracker] OI={oi:.2f} | baseline={baseline_oi:.2f} | "
            f"Δ={change_pct:+.2f}% | direction={direction}"
        )

        return {
            "current_oi": oi,
            "baseline_oi": baseline_oi,
            "oi_change_pct": change_pct,
            "direction": direction,
        }
