import logging
from datetime import datetime, timedelta
from collections import deque
from config.settings import PRICE_CHANGE_THRESHOLD_PCT, PRICE_WINDOW_MINUTES, ASSET

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

class PriceMonitor:
    def __init__(self):
        self.window_seconds = PRICE_WINDOW_MINUTES * 60
        self.threshold_pct = PRICE_CHANGE_THRESHOLD_PCT
        self.history = deque()

    def _prune_old(self):
        cutoff = datetime.utcnow() - timedelta(seconds=self.window_seconds)
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def tick(self):
        ctx = fetch_xyz_asset_ctx()
        if ctx is None:
            logger.error("[PriceMonitor] Could not fetch xyz:NVDA ctx")
            return None

        price = float(ctx.get("markPx") or ctx.get("midPx") or 0)
        if not price:
            return None

        now = datetime.utcnow()
        self.history.append((now, price))
        self._prune_old()

        if len(self.history) < 2:
            logger.debug(f"[PriceMonitor] price={price}, building history...")
            return None

        window_start_price = self.history[0][1]
        change_pct = ((price - window_start_price) / window_start_price) * 100

        logger.info(f"[PriceMonitor] {ASSET} price={price:.4f} | window_start={window_start_price:.4f} | delta={change_pct:+.2f}%")

        if abs(change_pct) >= self.threshold_pct:
            logger.info(f"[PriceMonitor] THRESHOLD BREACHED {change_pct:+.2f}%")
            return {
                "asset": ASSET,
                "current_price": price,
                "window_start_price": window_start_price,
                "price_change_pct": change_pct,
                "triggered_at": now,
            }
        return None
