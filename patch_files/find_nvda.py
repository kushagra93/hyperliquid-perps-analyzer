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

# Print all markets with NV in the name
print("NV matches:")
for i, m in enumerate(markets):
    if "NV" in m["name"].upper():
        print(f"  index={i} name={m['name']} ctx={ctxs[i] if i < len(ctxs) else 'NO CTX'}")

# Also print all market names so we can scan
print("\nAll market names:")
for m in markets:
    print(" ", m["name"])
