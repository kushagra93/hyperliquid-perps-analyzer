import os

fetch_func = '''
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
'''

# Rewrite price_monitor.py
price_monitor = '''import logging
from datetime import datetime, timedelta
from collections import deque
from config.settings import PRICE_CHANGE_THRESHOLD_PCT, PRICE_WINDOW_MINUTES, ASSET

logger = logging.getLogger(__name__)
''' + fetch_func + '''
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
'''

# Rewrite oi_tracker.py
oi_tracker = '''import logging
from datetime import datetime, timedelta
from collections import deque
from config.settings import OI_WINDOW_HOURS, ASSET

logger = logging.getLogger(__name__)
''' + fetch_func + '''
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
'''

# Rewrite agent2_oi.py
agent2 = '''import logging
from config.settings import ASSET

logger = logging.getLogger(__name__)
''' + fetch_func + '''
def build_oi_report(oi_snapshot: dict) -> dict:
    ctx = fetch_xyz_asset_ctx()
    report = {
        "current_oi": oi_snapshot["current_oi"],
        "baseline_oi": oi_snapshot["baseline_oi"],
        "oi_change_pct": oi_snapshot["oi_change_pct"],
        "oi_direction": oi_snapshot["direction"],
        "funding_rate": float(ctx["funding"]) if ctx else 0.0,
        "volume_24h": float(ctx["dayNtlVlm"]) if ctx else 0.0,
        "premium": float(ctx["premium"]) if ctx else 0.0,
    }

    oi_pct = oi_snapshot["oi_change_pct"]
    oi_dir = oi_snapshot["direction"]
    funding = report["funding_rate"]
    funding_bias = "bullish" if funding > 0 else "bearish" if funding < 0 else "neutral"

    report["interpretation"] = (
        f"Open interest moved {oi_pct:+.2f}% over 3 hours ({oi_dir}). "
        f"Current OI: {report['current_oi']:.2f}. "
        f"Funding: {funding * 100:.4f}% ({funding_bias}). "
        f"24h volume: ${report['volume_24h']:,.0f}."
    )
    logger.info(f"[Agent2] {report['interpretation']}")
    return report
'''

files = {
    "core/price_monitor.py": price_monitor,
    "core/oi_tracker.py": oi_tracker,
    "agents/agent2_oi.py": agent2,
}

for path, content in files.items():
    with open(path, "w") as f:
        f.write(content)
    print(f"Rewrote {path}")

print("\nDone. Run: python3 main.py")
