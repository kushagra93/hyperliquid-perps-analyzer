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

# Print all markets that contain token index 408
print("Markets containing token 408:")
for i, m in enumerate(markets):
    token_list = m.get("tokens", [])
    if 408 in token_list:
        print(f"  market_index={i} name={m['name']} tokens={token_list}")

# Also try searching tokens list directly for index 408
tokens = data[0].get("tokens", [])
print("\nToken at index position 408 in token list:")
for t in tokens:
    if t.get("index") == 408:
        print(" ", t)

# Print token indices around 408
print("\nTokens with index 400-410:")
for t in tokens:
    if 400 <= t.get("index", 0) <= 420:
        print(" ", t)
