import requests, urllib3
urllib3.disable_warnings()

# xyz deploys their own perps via a separate meta endpoint
# The asset ctx should contain NVDA if we look at all perp ctxs including deployer ones
resp = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "metaAndAssetCtxs"},
    timeout=10,
    verify=False
)
data = resp.json()
meta = data[0]
ctxs = data[1]

print(f"Total perp assets: {len(meta.get('universe', []))}")

# Print all assets
for i, asset in enumerate(meta.get("universe", [])):
    name = asset.get("name", "")
    if any(x in name.upper() for x in ["NV", "TSLA", "AAPL", "GOOGL"]):
        print(f"index={i} name={name} ctx={ctxs[i] if i < len(ctxs) else 'N/A'}")
