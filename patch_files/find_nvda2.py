import requests, urllib3
urllib3.disable_warnings()

resp = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "spotMetaAndAssetCtxs"},
    timeout=10,
    verify=False
)
data = resp.json()
tokens = data[0].get("tokens", [])
markets = data[0].get("universe", [])
ctxs = data[1]

# Find NVDA in tokens
print("NVDA token:")
nvda_token_index = None
for t in tokens:
    if "NV" in t["name"].upper():
        print(" ", t)
        nvda_token_index = t["index"]

# Find which market uses this token
print("\nMarket using NVDA token:")
if nvda_token_index is not None:
    for i, m in enumerate(markets):
        if nvda_token_index in m.get("tokens", []):
            print(f"  market index={i} name={m['name']}")
            if i < len(ctxs):
                print(f"  ctx={ctxs[i]}")
