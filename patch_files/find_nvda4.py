import requests, urllib3
urllib3.disable_warnings()

resp = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "spotMetaAndAssetCtxs"},
    timeout=10,
    verify=False
)
data = resp.json()
markets = data[0].get("universe", [])
ctxs = data[1]

print(f"Total markets: {len(markets)}")
print(f"Total ctxs: {len(ctxs)}")

# Search ctxs directly for NVDA coin name
print("\nSearching ctxs for NVDA:")
for i, ctx in enumerate(ctxs):
    if "NV" in ctx.get("coin", "").upper():
        print(f"  index={i} ctx={ctx}")

# Also check last 10 ctxs
print("\nLast 5 ctxs:")
for i, ctx in enumerate(ctxs[-5:]):
    print(f"  index={len(ctxs)-5+i} coin={ctx.get('coin')} markPx={ctx.get('markPx')}")
