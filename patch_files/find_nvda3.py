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

for i, m in enumerate(markets):
    if 408 in m.get("tokens", []):
        print(f"Market: index={i} name={m['name']}")
        if i < len(ctxs):
            print(f"Ctx: {ctxs[i]}")
