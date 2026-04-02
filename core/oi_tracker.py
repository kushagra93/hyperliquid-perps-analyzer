import logging
from datetime import datetime, timedelta
from collections import deque
from config.settings import OI_WINDOW_HOURS, ASSET

logger = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_DEX = "xyz"
HL_ASSET = "xyz:NVDA"

def fetch_xyz_asset_ctx():
    import requests, urllib3
    urllib3.disable_warnings()
    resp = requests.post(
        HL_INFO_URL,
        json={"type": "metaAndAssetCtxs", "dex": HL_DEX},
        timeout=10,
        verify=False
    )
    resp.raise_for_status()
    data = resp.json()
    universe = data[0].get("universe", [])
    ctxs = data[1]
    for i, asset in enumerate(universe):
        if asset.get("name") == HL_ASSET and i < len(ctxs):
            return ctxs[i]
    return None

class OITracker:
    def __init__(self):
        self.window_seconds = OI_WINDOW_HOURS * 3600
        self.history = deque()

    def _prune_old(self):
        cutoff = datetime.utcnow() - timedelta(seconds=self.window_seconds)
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def tick(self):
        ctx = fetch_xyz_asset_ctx()
        if ctx is None:
            logger.error("[OITracker] Could not fetch xyz:NVDA ctx")
            return None

        oi = float(ctx.get("openInterest") or 0)
        if not oi:
            return None

        now = datetime.utcnow()
        self.history.append((now, oi))
        self._prune_old()

        if len(self.history) < 2:
            return {"current_oi": oi, "baseline_oi": oi, "oi_change_pct": 0.0, "direction": "flat"}

        baseline_oi = self.history[0][1]
        change_pct = ((oi - baseline_oi) / baseline_oi) * 100 if baseline_oi else 0.0

        if change_pct > 0.5:
            direction = "up"
        elif change_pct < -0.5:
            direction = "down"
        else:
            direction = "flat"

        logger.info(f"[OITracker] OI={oi:.2f} | baseline={baseline_oi:.2f} | delta={change_pct:+.2f}% | dir={direction}")
        return {"current_oi": oi, "baseline_oi": baseline_oi, "oi_change_pct": change_pct, "direction": direction}
