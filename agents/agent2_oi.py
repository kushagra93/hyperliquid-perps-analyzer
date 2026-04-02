import logging
from config.settings import ASSET

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
