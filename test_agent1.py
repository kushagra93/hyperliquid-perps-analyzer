import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

from agents.agent1_news import fetch_news

print("\n" + "="*60)
print("AGENT 1 — NEWS REPORT")
print("="*60)

report = fetch_news()

print(f"\nHas news: {report['has_news']}")
print(f"Article count: {len(report['articles'])}")

print("\n--- Headlines ---")
for a in report['articles']:
    print(f"  [{a['source']}] {a['title']}")
    print(f"  Published: {a['published_at'][:19]}")
    if a.get('body'):
        print(f"  Body preview: {a['body'][:150]}...")
    print()

print("--- LLM Summary ---")
print(report['summary'])
print("="*60)
