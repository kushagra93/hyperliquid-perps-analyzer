# core/condition_engine.py
# ─────────────────────────────────────────────────────────────────
# Evaluates C1-C4 from price + OI direction.
#
# Fix 2: should_alert now allows volume-triggered C3/C4 through.
# A volume-only trigger on C3/C4 is treated as a signal worth
# alerting on, consistent with volume being a first-class trigger.
# ─────────────────────────────────────────────────────────────────

import logging

logger = logging.getLogger(__name__)

CONDITIONS = {
    "C1": {
        "label": "Strong bull",
        "description": "Price up + OI up — new longs entering, conviction move",
        "price_dir": "up",
        "oi_dir": "up",
        "always_alert": True,
    },
    "C2": {
        "label": "Strong bear",
        "description": "Price down + OI up — new shorts entering, conviction sell",
        "price_dir": "down",
        "oi_dir": "up",
        "always_alert": True,
    },
    "C3": {
        "label": "Weak fall",
        "description": "Price down + OI down — longs exiting, no fresh conviction",
        "price_dir": "down",
        "oi_dir": "down",
        "always_alert": False,
    },
    "C4": {
        "label": "Weak rally",
        "description": "Price up + OI down — shorts exiting, no fresh conviction",
        "price_dir": "up",
        "oi_dir": "down",
        "always_alert": False,
    },
}


def classify_price_direction(price_change_pct: float) -> str:
    if price_change_pct > 0:
        return "up"
    elif price_change_pct < 0:
        return "down"
    return "flat"


def evaluate_condition(price_trigger: dict, oi_snapshot: dict) -> dict | None:
    """
    Matches price + OI direction to C1-C4.
    If price is flat but volume triggered, uses volume direction as fallback.
    """
    price_dir = classify_price_direction(price_trigger["price_change_pct"])
    oi_dir = oi_snapshot["direction"]
    volume_trigger = price_trigger.get("volume_trigger")

    if price_dir == "flat" and volume_trigger is not None:
        volume_change_pct = volume_trigger.get("volume_change_pct", 0.0)
        if volume_change_pct > 0:
            price_dir = "up"
            logger.info(
                "[ConditionEngine] Price flat with volume-only trigger. "
                "Using positive volume direction as fallback price direction."
            )
        elif volume_change_pct < 0:
            price_dir = "down"
            logger.info(
                "[ConditionEngine] Price flat with volume-only trigger. "
                "Using negative volume direction as fallback price direction."
            )

    if price_dir == "flat" or oi_dir == "flat":
        logger.info("[ConditionEngine] No clear condition — price or OI direction is flat.")
        return None

    for cid, cdef in CONDITIONS.items():
        if cdef["price_dir"] == price_dir and cdef["oi_dir"] == oi_dir:
            result = {
                "condition_id": cid,
                "label": cdef["label"],
                "description": cdef["description"],
                "always_alert": cdef["always_alert"],
                "price_change_pct": price_trigger["price_change_pct"],
                "oi_change_pct": oi_snapshot["oi_change_pct"],
                "volume_change_pct": (
                    volume_trigger.get("volume_change_pct") if volume_trigger else None
                ),
                "trigger_source": price_trigger.get("trigger_source", "price"),
            }
            logger.info(f"[ConditionEngine] Matched {cid}: {cdef['label']}")
            return result

    logger.info(f"[ConditionEngine] No matching condition for price_dir={price_dir}, oi_dir={oi_dir}")
    return None


def should_alert(condition: dict, news_report: dict) -> bool:
    """
    Alert rules:
    - C1 / C2: always alert
    - C3 / C4: alert if news found OR triggered by volume
      (volume-triggered C3/C4 = unusual flow, worth surfacing)
    """
    if condition["always_alert"]:
        logger.info(f"[ConditionEngine] {condition['condition_id']} — always alert. Firing.")
        return True

    has_news = news_report.get("has_news", False)
    if has_news:
        logger.info(f"[ConditionEngine] {condition['condition_id']} — news found. Firing.")
        return True

    # Fix 2: volume-triggered C3/C4 should also fire
    trigger_source = condition.get("trigger_source", "price")
    volume_triggered = "volume" in trigger_source
    if volume_triggered:
        logger.info(
            f"[ConditionEngine] {condition['condition_id']} — "
            f"volume trigger (source={trigger_source}). Firing."
        )
        return True

    logger.info(
        f"[ConditionEngine] {condition['condition_id']} — "
        "no news, no volume trigger. Suppressing alert."
    )
    return False
