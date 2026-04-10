import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
import urllib3

from config.settings import (
    ASSET,
    HL_PERP_DEX,
    VOLUME_CHANGE_THRESHOLD_PCT,
    VOLUME_WINDOW_MINUTES,
)
from core.hl_client import fetch_meta_and_asset_ctxs

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# If 24h volume drops by more than this % between ticks,
# treat it as a daily reset and skip the delta for that tick.
RESET_DETECTION_DROP_PCT = 30.0


def fetch_notional_volume_24h(asset: str) -> float | None:
    """Fetch the current 24h notional volume (dayNtlVlm) for an asset."""
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
                return float(asset_ctxs[i].get("dayNtlVlm", 0))

        logger.warning(f"Asset '{asset}' not found in HL universe for volume (dex={HL_PERP_DEX or 'default'}).")
        return None
    except Exception as e:
        logger.error(f"Error fetching 24h notional volume: {e}")
        return None


class VolumeMonitor:
    """
    Tracks per-tick volume deltas over a rolling window.

    Instead of comparing raw 24h snapshots (which resets daily
    causing false spikes), we compute the incremental volume
    added each tick and sum those deltas over the window.

    Trigger fires when the accumulated window delta as a % of
    the window-start 24h volume breaches VOLUME_CHANGE_THRESHOLD_PCT.
    """

    def __init__(self):
        self.window_seconds = VOLUME_WINDOW_MINUTES * 60
        self.threshold_pct = VOLUME_CHANGE_THRESHOLD_PCT
        # Store (timestamp, volume_24h, tick_delta) tuples
        self.history: deque = deque()
        self._last_volume: float | None = None

    def _prune_old(self):
        cutoff = datetime.now(IST) - timedelta(seconds=self.window_seconds)
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def _is_daily_reset(self, prev: float, curr: float) -> bool:
        """Detect a daily reset: volume dropped sharply between ticks."""
        if prev <= 0:
            return False
        drop_pct = ((prev - curr) / prev) * 100
        return drop_pct >= RESET_DETECTION_DROP_PCT

    def tick(self) -> dict | None:
        """
        Returns a trigger dict if threshold is breached, else None.
        {
            "asset": str,
            "current_volume": float,
            "window_start_volume": float,
            "volume_change_pct": float,
            "triggered_at": datetime,
        }
        """
        volume = fetch_notional_volume_24h(ASSET)
        if volume is None:
            return None

        now = datetime.now(IST)

        # Compute tick delta — skip if this looks like a daily reset
        tick_delta = 0.0
        if self._last_volume is not None:
            if self._is_daily_reset(self._last_volume, volume):
                logger.info(
                    f"[VolumeMonitor] Daily reset detected "
                    f"({self._last_volume:.0f} → {volume:.0f}). Skipping delta."
                )
                tick_delta = 0.0
            else:
                tick_delta = max(volume - self._last_volume, 0.0)

        self._last_volume = volume
        self.history.append((now, volume, tick_delta))
        self._prune_old()

        if len(self.history) < 2:
            logger.debug(f"[VolumeMonitor] Tick — volume={volume:.2f}, building history.")
            return None

        # Window baseline = volume at oldest tick in window
        window_start_volume = self.history[0][1]

        # Accumulated delta = sum of tick deltas in the window (excluding first)
        window_delta = sum(d for _, _, d in list(self.history)[1:])

        if window_start_volume <= 0:
            logger.warning("[VolumeMonitor] Baseline volume is 0. Skipping.")
            return None

        change_pct = (window_delta / window_start_volume) * 100

        logger.info(
            f"[VolumeMonitor] {ASSET} vol24h={volume:.0f} | "
            f"window_delta={window_delta:.0f} | "
            f"window_start={window_start_volume:.0f} | "
            f"Δ={change_pct:+.2f}%"
        )

        if abs(change_pct) >= self.threshold_pct:
            logger.info(f"[VolumeMonitor] THRESHOLD BREACHED — Δ={change_pct:+.2f}%")
            return {
                "asset": ASSET,
                "current_volume": volume,
                "window_start_volume": window_start_volume,
                "volume_change_pct": change_pct,
                "triggered_at": now,
            }

        return None
