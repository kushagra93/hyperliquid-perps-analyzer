import requests, urllib3
urllib3.disable_warnings()

# xyz's own HL instance has a separate API endpoint
resp = requests.post(
    "https://api.hyperliquid-testnet.xyz/info",
    json={"type": "metaAndAssetCtxs"},
    timeout=10,
    verify=False
)
print("Status:", resp.status_code)
data = resp.json()
meta = data[0]
ctxs = data[1]
for i, asset in enumerate(meta.get("universe", [])):
    if "NV" in asset.get("name","").upper():
        print(f"index={i} name={asset['name']} ctx={ctxs[i]}")
