import requests, urllib3
urllib3.disable_warnings()

def fetch_spot_asset(asset):
    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "spotMetaAndAssetCtxs"},
        timeout=10,
        verify=False
    )
    resp.raise_for_status()
    data = resp.json()
    markets = data[0].get("universe", [])
    ctxs = data[1]
    search = [asset.upper(), asset.upper() + "/USDC"]
    for i, m in enumerate(markets):
        if m["name"].upper() in search and i < len(ctxs):
            ctx = ctxs[i]
            mark_px = float(ctx.get("markPx") or ctx.get("midPx") or 0)
            volume = float(ctx.get("dayNtlVlm") or 0)
            return mark_px, volume
    return None, None

mark, vol = fetch_spot_asset("NVDA")
print("NVDA mark price:", mark)
print("NVDA 24h volume:", vol)
