import requests, urllib3
urllib3.disable_warnings()

# First get list of perp dexes
resp = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "perpDexs"},
    timeout=10,
    verify=False
)
print("Perp dexes:", resp.json())

# Then query xyz dex specifically
resp2 = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "metaAndAssetCtxs", "dex": "xyz"},
    timeout=10,
    verify=False
)
data = resp2.json()
meta = data[0]
ctxs = data[1]

print(f"\nTotal xyz assets: {len(meta.get('universe', []))}")
for i, asset in enumerate(meta.get("universe", [])):
    if "NV" in asset.get("name", "").upper():
        print(f"index={i} asset={asset}")
        print(f"ctx={ctxs[i] if i < len(ctxs) else 'N/A'}")
