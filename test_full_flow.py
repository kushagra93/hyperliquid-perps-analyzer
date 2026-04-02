import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

from datetime import datetime
from core.condition_engine import evaluate_condition, should_alert
from agents.agent1_news import fetch_news
from agents.agent2_oi import build_oi_report
from agents.agent3_causality import run_causality_analysis
from notifiers.sheets import log_alert

# Fake price trigger — C1 setup (price up)
price_trigger = {
    "asset": "NVDA",
    "current_price": 175.86,
    "window_start_price": 171.57,
    "price_change_pct": 2.5,
    "triggered_at": datetime.utcnow(),
}

# Force C1: price up + OI up
oi_snapshot = {
    "current_oi": 329000.0,
    "baseline_oi": 310000.0,
    "oi_change_pct": 6.1,
    "direction": "up"
}

print("\n--- Step 2: Evaluating condition ---")
condition = evaluate_condition(price_trigger, oi_snapshot)
print(f"Condition: {condition}")

if condition is None:
    print("No condition matched — check price/OI dtions")
    exit(1)

print("\n--- Step 3: Agent 1 - News ---")
news_report = fetch_news()
print(f"Has news: {news_report['has_news']} | Articles: {len(news_report['articles'])}")

print("\n--- Step 4: Agent 2 - OI Report ---")
oi_report = build_oi_report(oi_snapshot)
print(f"Interpretation: {oi_report['interpretation']}")

print("\n--- Step 5: Should alert? ---")
alert = should_alert(condition, news_report)
print(f"Alert: {alert}")

if alert:
    print("\n--- Step 6: Agent 3 - Causality ---")
    causality = run_causality_analysis(price_trigger, news_report, oi_report, condition)
    print(f"Verdict   : {causality.get('verdict')}")
    print(f"Confidence: {causality.get('confidence')}")
    print(f"Driver    : {causality.get('primary_driver')}")
    print(f"Reasoning : {causality.get('reasoning')}")

    print("\n--- Step 7: Logging to Google Sheets ---")
    log_alert(price_trigger, oi_report, condition, causality, news_report)
    print("Done — check your Google Sheet!")
else:
    print("Alert suppresseby condition rules.")
