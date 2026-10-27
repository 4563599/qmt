# -*- coding: gbk -*-
"""Fast intraday PZ scanner for QMT.

The source is intentionally ASCII-only.  ASCII is valid GBK and avoids source
damage when QMT or an editor opens this file with the system code page.
"""

from __future__ import print_function

import base64
import json
import math
import os
import queue
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

try:
    import redis
except Exception:
    redis = None


# ==================== configuration ====================

REFERENCE_DATE = ""
CHECK_TRADING_TIME = True

MIN_REALTIME_GAIN_PCT = 5.0
HIGH_GAIN_THRESHOLD_PCT = 6.5
MAX_YIN_COUNT_20 = 7
MIN_HIGH_GAIN_COUNT_10D = 5
MIN_LOW_ABOVE_MA5_COUNT_10D = 5
PZ1_MAX_YIN_COUNT_20 = 7

HISTORY_REQUEST_DAYS = 65
KLINE_TOTAL_DAYS = 60
KLINE_HISTORY_DAYS = KLINE_TOTAL_DAYS - 1
HISTORY_BATCH_SIZE = 300
TIMER_INTERVAL_TEXT = "1nSecond"

REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_DB = 0
REDIS_TTL_SEC = 30 * 24 * 60 * 60

REDIS_KEY_PREFIX = "pz"
PZ1_REDIS_KEY_PREFIX = "pz1"
QY1_REDIS_KEY_PREFIX = "qy1"
QY2_REDIS_KEY_PREFIX = "qy2"
LIMIT_UP_LADDER_PREFIX = "limitup:ladder"
KLINE_HISTORY_PREFIX = "pz:kline:history"
KLINE_REALTIME_PREFIX = "pz:kline:realtime"
QY1_KLINE_HISTORY_PREFIX = "qy1:kline:history"
QY1_KLINE_REALTIME_PREFIX = "qy1:kline:realtime"
QY2_KLINE_HISTORY_PREFIX = "qy2:kline:history"
QY2_KLINE_REALTIME_PREFIX = "qy2:kline:realtime"
STATUS_KEY_PREFIX = "pz:status"
PERFORMANCE_KEY_PREFIX = "pz:performance"
DINGTALK_DEDUP_PREFIX = "dingtalk:pz:pushed"
DINGTALK_CONDITION3_DEDUP_PREFIX = "dingtalk:pz:pushed:condition3"
DINGTALK_RECORD_PREFIX = "dingtalk:pz:records"

DINGTALK_ENABLED = True
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")
DINGTALK_CONDITION3_WEBHOOK = os.environ.get("DINGTALK_CONDITION3_WEBHOOK", "")
DINGTALK_ROUTE_YIN20 = "yin20"
DINGTALK_ROUTE_CONDITION3 = "condition3"
DINGTALK_FIVE_PULL_CONDITIONS = (
    ("qiangchi", "1.5\u62c9\u5f3a\u5403"),
    ("five_pull_all_yin_up", "2.5\u62c9\u9634\u7ebf\u90fd\u5411\u4e0a"),
    ("five_pull_all_yin_eaten_next", "3.5\u62c9\u9634\u88ab\u5403"),
    ("five_pull_yin_pull_next", "4.5\u62c9\u9634\u7ebf\u540e\u62c9"),
    (
        "five_pull_all_yin_cross_star",
        "5.5\u62c9\u9634\u7ebf\u90fd\u662f\u5341\u5b57\u661f",
    ),
    ("is_quanyang_10d", "6.\u5168\u9633"),
)
DINGTALK_RETRY_COUNT = 3
DINGTALK_PERSIST_RETRY_COUNT = 3
DINGTALK_PERSIST_RETRY_MAX_DELAY_SEC = 30
DINGTALK_MAX_YIN_COUNT_20 = 4
DINGTALK_SECURITY_KEYWORD = "\u80a1\u7968\u9884\u8b66"
LOG_ENABLED = True
LOG_SELECTED_LIMIT = 10

LOG_BASE_DIR = "C:\\Users\\lyy\\Desktop\\%s\\%s" % (
    "\u80a1\u7968",
    "\u9884\u8b66",
)
STRATEGY_DIR_NAME = "\u76d8\u4e2d"


# ==================== runtime state ====================

g_context = None
g_redis_client = None
g_initialized = False
g_stock_pool = []
g_stock_names = {}
g_ladder_pool = []
g_ladder_details = {}
g_ladder_details_date = ""
g_ladder_history = {}
g_ladder_history_date = ""
g_ladder_history_retry_at = {}
g_history_data = {}
g_reference_date = ""
g_history_reload_date = ""
g_scan_lock = threading.Lock()
g_scan_count = 0
g_busy_skip_count = 0
g_historical_done = False
g_last_cycle_started_ts = 0.0

g_dingtalk_queue = queue.Queue(maxsize=1000)
g_dingtalk_lock = threading.Lock()
g_dingtalk_sent = set()
g_dingtalk_pending = set()
g_dingtalk_loaded_date = ""
g_dingtalk_persist_queue = queue.Queue()
g_dingtalk_persist_pending = set()
g_history_published_date = ""
g_history_published_codes = set()
g_qy1_history_published_date = ""
g_qy1_history_published_codes = set()
g_qy2_history_published_date = ""
g_qy2_history_published_codes = set()
g_pz1_candidate_cache_date = ""
g_pz1_candidate_stocks = set()
g_qy2_candidate_cache_date = ""
g_qy2_candidate_stocks = set()
g_reference_series_cache_date = ""
g_reference_series_cache = {}
g_window_metrics_cache_date = ""
g_window_metrics_cache = {}


# ==================== pure helpers ====================

def safe_float(value, default=0.0):
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return float(default)
        return result
    except Exception:
        return float(default)


def previous_close_from_tick(tick, history_close=0.0, older_history_close=0.0):
    """Reject a tick close confirmed to belong to the older history session.

    Keep other quote/history differences: they may be ex-right reference
    prices. Never replace an official quote merely because it differs.
    """
    tick = tick if isinstance(tick, dict) else {}
    last_close = safe_float(tick.get("lastClose"))
    pre_close = safe_float(tick.get("preClose"))
    if last_close > 0:
        previous, source = last_close, "lastClose"
    elif pre_close > 0:
        previous, source = pre_close, "preClose"
    else:
        previous, source = 0.0, "none"
    history_close = safe_float(history_close)
    older_history_close = safe_float(older_history_close)
    if history_close > 0:
        if previous <= 0:
            return history_close, "history"
        if (
            older_history_close > 0
            and abs(previous - older_history_close) <= 1e-8
            and abs(previous - history_close) > 1e-8
        ):
            return history_close, "history_stale_tick"
    return previous, source


def previous_history_closes(history, ref_date):
    """Read the last two validated bars strictly before the reference date."""
    dates = history.get("dates") or []
    closes = history.get("closes") or []
    previous = []
    for index in range(min(len(dates), len(closes)) - 1, -1, -1):
        if dates[index] >= ref_date:
            continue
        close_price = safe_float(closes[index])
        if close_price > 0:
            previous.append(close_price)
            if len(previous) == 2:
                break
    return (
        previous[0] if previous else 0.0,
        previous[1] if len(previous) > 1 else 0.0,
    )


@lru_cache(maxsize=512)
def validated_trade_date(text):
    try:
        datetime.strptime(text, "%Y%m%d")
        return text
    except Exception:
        return ""


def normalize_trade_date(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return ""
    text = digits[:8]
    return validated_trade_date(text)


def tick_trade_date(tick):
    tick = tick if isinstance(tick, dict) else {}
    for field in ("time", "timetag", "datetime"):
        raw_value = tick.get(field)
        if raw_value is None or raw_value == "":
            continue
        text = str(raw_value).strip()
        compact_date = text.isdigit() and len(text) in (8, 14, 17)
        separated_date = len(text) >= 10 and text[4] in "-/" and text[7] in "-/"
        qmt_date = len(text) > 8 and text[:8].isdigit() and text[8] in " T"
        # Epoch milliseconds can start with a valid-looking date in year 1793.
        if (compact_date or separated_date or qmt_date) and text[:4].isdigit():
            if 1990 <= int(text[:4]) <= 2199:
                direct = normalize_trade_date(text)
                if direct:
                    return direct
        numeric = safe_float(raw_value)
        if numeric <= 0:
            continue
        if numeric > 100000000000000000:
            numeric /= 1000000000.0
        elif numeric > 100000000000000:
            numeric /= 1000000.0
        elif numeric > 100000000000:
            numeric /= 1000.0
        try:
            value = datetime.utcfromtimestamp(numeric) + timedelta(hours=8)
            if 1990 <= value.year <= 2199:
                return value.strftime("%Y%m%d")
        except Exception:
            pass
    return ""


def calc_gain(value, previous):
    value = safe_float(value)
    previous = safe_float(previous)
    if previous <= 0:
        return 0.0
    return (value - previous) / previous * 100.0


def get_limit_up_ratio(stock_code):
    code = simple_code(stock_code)
    if code.startswith("688") or code.startswith("300") or code.startswith("301"):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def calc_limit_up_price(previous_close, stock_code):
    previous_close = safe_float(previous_close)
    if previous_close <= 0:
        return 0.0
    return round(previous_close * (1.0 + get_limit_up_ratio(stock_code) / 100.0), 2)


def is_limit_up_by_price(close_price, previous_close, stock_code):
    limit_price = calc_limit_up_price(previous_close, stock_code)
    return limit_price > 0 and safe_float(close_price) >= limit_price - 0.01


def calc_limit_down_price(previous_close, stock_code):
    previous_close = safe_float(previous_close)
    if previous_close <= 0:
        return 0.0
    ratio = Decimal(str(get_limit_up_ratio(stock_code))) / Decimal("100")
    limit_price = Decimal(str(previous_close)) * (Decimal("1") - ratio)
    return float(limit_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_limit_down_by_price(close_price, previous_close, stock_code):
    limit_price = calc_limit_down_price(previous_close, stock_code)
    close_price = safe_float(close_price)
    return limit_price > 0 and 0 < close_price <= limit_price + 1e-8


def is_nearly_bare_foot(open_price, close_price, high_price, low_price):
    open_price = safe_float(open_price)
    close_price = safe_float(close_price)
    low_price = safe_float(low_price)
    if min(open_price, close_price, low_price) <= 0 or close_price < open_price:
        return False
    bottom = min(open_price, close_price)
    lower_shadow = bottom - low_price
    if lower_shadow <= 0:
        return True
    if lower_shadow / bottom * 100.0 < 0.1:
        return True
    return abs(low_price - open_price) <= 0.01


def calculate_prev3_shadow_metrics(
    opens, highs, lows, closes, reference_index=None
):
    size = min(len(opens), len(highs), len(lows), len(closes))
    ref_index = size - 1 if reference_index is None else int(reference_index)
    ref_index = max(0, min(ref_index, size))
    indexes = list(range(max(0, ref_index - 3), ref_index))
    yang_count = 0
    all_short = len(indexes) == 3
    for index in indexes:
        open_price = safe_float(opens[index])
        high_price = safe_float(highs[index])
        low_price = safe_float(lows[index])
        close_price = safe_float(closes[index])
        if min(open_price, high_price, low_price, close_price) <= 0:
            all_short = False
            continue
        if close_price > open_price:
            yang_count += 1
        upper_shadow_pct = (
            max(0.0, high_price - max(open_price, close_price))
            / close_price
            * 100.0
        )
        lower_shadow_pct = (
            max(0.0, min(open_price, close_price) - low_price)
            / close_price
            * 100.0
        )
        if not (
            upper_shadow_pct <= 0.5 + 1e-9
            or lower_shadow_pct <= 0.5 + 1e-9
        ):
            all_short = False
    return {
        "prev3_short_shadow_met": int(all_short),
        "prev3_yang_count": int(yang_count),
    }


def realtime_gain_from_tick(tick, previous_close=None):
    tick = tick if isinstance(tick, dict) else {}
    price = safe_float(tick.get("lastPrice") or tick.get("price"))
    if previous_close is None:
        previous, _ = previous_close_from_tick(tick)
    else:
        previous = safe_float(previous_close)
    return calc_gain(price, previous)


def passes_realtime_gain_gate(tick, threshold=5.0, previous_close=None):
    return realtime_gain_from_tick(tick, previous_close) >= safe_float(threshold, 5.0)


def passes_secondary_conditions(
    yin_count_20,
    high_gain_10d_count,
    all_close_above_ma10_10d,
    low_above_ma5_count_10d,
    five_pull_group_flags=None,
):
    reasons = []
    if int(yin_count_20) <= MAX_YIN_COUNT_20:
        reasons.append("yin_count_20_lte_%d" % MAX_YIN_COUNT_20)
    if (
        int(high_gain_10d_count) >= 5
        and int(all_close_above_ma10_10d) == 1
    ):
        reasons.append("high_gain5_and_all_close_above_ma10")
    if int(low_above_ma5_count_10d) >= 5:
        reasons.append("low_above_ma5_10d_gte_5")
    for field in (
        "five_pull_all_yin_no_touch",
        "five_pull_all_yin_up",
        "five_pull_all_yin_cross_star",
        "five_pull_yin_pull_next",
        "five_pull_all_yin_eaten_next",
    ):
        if int((five_pull_group_flags or {}).get(field, 0) or 0) == 1:
            reasons.append(field)
    return bool(reasons), reasons


def calculate_five_pull_group_flags(metrics):
    metrics = metrics if isinstance(metrics, dict) else {}
    high_gain_ok = int(metrics.get("high_gain_10d_count", 0) or 0) >= 5
    return {
        "five_pull_all_yin_no_touch": int(
            high_gain_ok and int(metrics.get("all_yin_no_touch", 0) or 0) == 1
        ),
        "five_pull_all_yin_up": int(
            high_gain_ok and int(metrics.get("all_yin_up", 0) or 0) == 1
        ),
        "five_pull_all_yin_cross_star": int(
            high_gain_ok and int(metrics.get("all_yin_cross_star", 0) or 0) == 1
        ),
        "five_pull_yin_pull_next": int(
            high_gain_ok and int(metrics.get("yin_pull_next", 0) or 0) == 1
        ),
        "five_pull_all_yin_eaten_next": int(
            high_gain_ok and int(metrics.get("all_yin_eaten_next", 0) or 0) == 1
        ),
    }


def calculate_yi_yin_yi_yang_flags(
    stock_code, opens, closes, high_gain_10d_count, reference_previous_close=0.0
):
    """Check non-yin successors by close, including flat limit-down candles."""
    unmatched = {"yi_yin_yi_yang": 0, "qiangchi": 0}
    size = min(len(opens), len(closes))
    if size < 10 or int(high_gain_10d_count) < MIN_HIGH_GAIN_COUNT_10D:
        return unmatched
    recent_opens = [safe_float(value) for value in opens[size - 10:size]]
    recent_closes = [safe_float(value) for value in closes[size - 10:size]]
    if min(recent_opens + recent_closes) <= 0:
        return unmatched
    previous_close = safe_float(reference_previous_close)
    if previous_close <= 0:
        previous_close = recent_closes[-2]
    if (
        recent_closes[-1] < recent_opens[-1]
        or is_limit_down_by_price(recent_closes[-1], previous_close, stock_code)
    ):
        return unmatched

    qiangchi = 1
    for index in range(9):
        if recent_closes[index] >= recent_opens[index]:
            continue
        next_open = recent_opens[index + 1]
        next_close = recent_closes[index + 1]
        if (
            next_close < next_open
            or is_limit_down_by_price(next_close, recent_closes[index], stock_code)
        ):
            return unmatched
        if next_close < recent_opens[index]:
            qiangchi = 0
    return {"yi_yin_yi_yang": 1, "qiangchi": qiangchi}


def calculate_reference_candle_flags(
    stock_code, opens, closes, reference_previous_close=0.0
):
    """Count the current board streak and check windows without a five-pull gate."""
    result = {
        "is_first_limit_up": 0,
        "is_second_limit_up": 0,
        "consecutive_limit_up_days": 0,
        "yi_yin_yi_yang_10d": 0,
        "yi_yin_yi_yang_20d": 0,
    }
    size = len(closes)
    if not size:
        return result
    previous_close = safe_float(reference_previous_close)
    if previous_close <= 0 and size >= 2:
        previous_close = safe_float(closes[-2])

    streak = 0
    boundary_known = False
    for index in range(size - 1, -1, -1):
        day_close = safe_float(closes[index])
        day_previous = (
            previous_close if index == size - 1
            else safe_float(closes[index - 1]) if index >= 1 else 0.0
        )
        if min(day_close, day_previous) <= 0:
            break
        if not is_limit_up_by_price(day_close, day_previous, stock_code):
            boundary_known = True
            break
        streak += 1
    result["consecutive_limit_up_days"] = streak
    # Missing older data must not falsely label an exact first/second board.
    result["is_first_limit_up"] = int(boundary_known and streak == 1)
    result["is_second_limit_up"] = int(boundary_known and streak == 2)

    if size < 10 or len(opens) != size:
        return result
    current_open = safe_float(opens[-1])
    current_close = safe_float(closes[-1])
    if (
        min(current_open, current_close, previous_close) <= 0
        or current_close < current_open
        or is_limit_down_by_price(current_close, previous_close, stock_code)
    ):
        return result

    next_open, next_close = current_open, current_close
    for offset in range(1, min(size, 20) + 1):
        index = size - offset
        day_open = safe_float(opens[index])
        day_close = safe_float(closes[index])
        if min(day_open, day_close) <= 0:
            break
        if day_close < day_open:
            next_previous = previous_close if offset == 2 else day_close
            if (
                next_close < next_open
                or is_limit_down_by_price(next_close, next_previous, stock_code)
            ):
                break
        if offset in (10, 20):
            result["yi_yin_yi_yang_%dd" % offset] = 1
        next_open, next_close = day_open, day_close
    return result


def calculate_limit_up_gap_flags(stock_code, series):
    """Nearest earlier limit-up in 30 sessions; the intervening yin count is <=1."""
    result = {
        "limit_up_gap_yin_lte1_30d": 0,
        "prev_limit_up_date_30d": "",
        "limit_up_gap_yin_count_30d": None,
    }
    opens = series.get("opens") or []
    closes = series.get("closes") or []
    dates = series.get("dates") or []
    size = len(closes)
    if size < 2 or len(opens) != size:
        return result
    previous = safe_float(series.get("pre_close")) or safe_float(closes[-2])
    if min(safe_float(opens[-1]), safe_float(closes[-1]), previous) <= 0:
        return result
    multiplier = Decimal("1") + Decimal(str(get_limit_up_ratio(stock_code))) / Decimal("100")

    def at_limit(close_price, previous_close):
        upper = float((Decimal(str(previous_close)) * multiplier).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ))
        return upper > 0 and abs(safe_float(close_price) - upper) < 0.0001

    if not at_limit(closes[-1], previous):
        return result
    # Include the reference day in the 30-day window; exclude both endpoints
    # from the yin count. A prior close is required to judge the earlier board.
    first = max(1, size - 30)
    for index in range(size - 2, first - 1, -1):
        day_open = safe_float(opens[index])
        day_close = safe_float(closes[index])
        day_previous = safe_float(closes[index - 1])
        if min(day_open, day_close, day_previous) <= 0:
            return result
        if not at_limit(day_close, day_previous):
            continue
        count = sum(safe_float(closes[day]) < safe_float(opens[day])
                    for day in range(index + 1, size - 1))
        result.update({
            "limit_up_gap_yin_lte1_30d": int(count <= 1),
            "prev_limit_up_date_30d": normalize_trade_date(dates[index]) if index < len(dates) else "",
            "limit_up_gap_yin_count_30d": count,
        })
        return result
    return result


def summarize_condition_hits(results):
    summary = {
        "condition_2_count": 0,
        "condition_3_count": 0,
        "condition_4_count": 0,
        "multi_condition_count": 0,
    }
    for result in results or []:
        flags = [
            int(result.get("condition_2_met", 0) or 0),
            int(result.get("condition_3_met", 0) or 0),
            int(result.get("condition_4_met", 0) or 0),
        ]
        summary["condition_2_count"] += int(flags[0] == 1)
        summary["condition_3_count"] += int(flags[1] == 1)
        summary["condition_4_count"] += int(flags[2] == 1)
        summary["multi_condition_count"] += int(sum(flags) >= 2)
    return summary


def summarize_five_pull_group_hits(results):
    fields = (
        "five_pull_all_yin_no_touch",
        "five_pull_all_yin_up",
        "five_pull_all_yin_cross_star",
        "five_pull_yin_pull_next",
        "five_pull_all_yin_eaten_next",
    )
    return {
        "%s_count" % field: sum(
            int(result.get(field, 0) or 0) == 1 for result in (results or [])
        )
        for field in fields
    }


def is_cross_star(open_price, close_price, previous_close):
    previous_close = safe_float(previous_close)
    if previous_close <= 0:
        return False
    return abs(safe_float(close_price) - safe_float(open_price)) / previous_close * 100.0 <= 1.9


def build_reference_series(history, tick, ref_date, realtime=True, prepared=None):
    history = history if isinstance(history, dict) else {}
    tick = tick if isinstance(tick, dict) else {}
    ref_date = normalize_trade_date(ref_date)
    source_dates = list(history.get("dates") or []) if prepared is None else []
    fields = {
        name: list(history.get(name) or []) if prepared is None else []
        for name in ("opens", "highs", "lows", "closes", "volumes", "amounts")
    }
    size = min(
        [len(source_dates)]
        + [len(fields[name]) for name in ("opens", "highs", "lows", "closes")]
    )
    dates = []
    output = {name: [] for name in fields}
    for index in range(size):
        date_text = normalize_trade_date(source_dates[index])
        if not date_text or (ref_date and date_text > ref_date):
            continue
        values = {
            name: safe_float(fields[name][index]) if index < len(fields[name]) else 0.0
            for name in fields
        }
        if min(values["opens"], values["highs"], values["lows"], values["closes"]) <= 0:
            continue
        dates.append(date_text)
        for name in output:
            output[name].append(values[name])

    if prepared is not None:
        dates = list(prepared["dates"])
        output = {name: list(prepared[name]) for name in fields}

    history_last_date = dates[-1] if dates else ""
    existing_index = dates.index(ref_date) if ref_date in dates else -1
    previous_close, older_history_close = previous_history_closes(
        {"dates": dates, "closes": output["closes"]}, ref_date
    )
    previous_close_source = "history"

    if realtime and ref_date:
        existing = {}
        if existing_index >= 0:
            existing = {name: output[name][existing_index] for name in output}
        close_price = safe_float(tick.get("lastPrice") or tick.get("price"))
        pre_close, previous_close_source = previous_close_from_tick(
            tick, previous_close, older_history_close
        )
        if close_price <= 0:
            close_price = safe_float(existing.get("closes"))
        if pre_close <= 0:
            pre_close = previous_close
            previous_close_source = "history"
        open_price = safe_float(tick.get("open")) or safe_float(existing.get("opens")) or close_price
        high_price = safe_float(tick.get("high")) or safe_float(existing.get("highs")) or max(open_price, close_price)
        low_price = safe_float(tick.get("low")) or safe_float(existing.get("lows")) or min(open_price, close_price)
        high_price = max(high_price, open_price, close_price)
        positive_prices = [
            value for value in (low_price, open_price, close_price) if value > 0
        ]
        low_price = min(positive_prices) if positive_prices else 0.0
        values = {
            "opens": open_price,
            "highs": high_price,
            "lows": low_price,
            "closes": close_price,
            "volumes": safe_float(tick.get("volume") or tick.get("vol")) or safe_float(existing.get("volumes")),
            "amounts": safe_float(tick.get("amount")) or safe_float(existing.get("amounts")),
        }
        if min(values["opens"], values["highs"], values["lows"], values["closes"]) > 0:
            if existing_index >= 0:
                for name in output:
                    output[name][existing_index] = values[name]
            else:
                dates.append(ref_date)
                for name in output:
                    output[name].append(values[name])
                existing_index = len(dates) - 1
            previous_close = pre_close or previous_close

    if existing_index < 0 and ref_date in dates:
        existing_index = dates.index(ref_date)
    if existing_index >= 1 and previous_close <= 0:
        previous_close = output["closes"][existing_index - 1]
    elif previous_close <= 0 and existing_index < 0 and len(output["closes"]) >= 2:
        previous_close = output["closes"][-2]

    if previous_close <= 0:
        previous_close_source = "missing"
    result = {
        "dates": dates,
        "_ref_date": ref_date,
        "pre_close": safe_float(previous_close),
        "_pre_close_source": previous_close_source,
        "_history_last_date": history_last_date,
    }
    result.update(output)
    return result


def build_scan_reference_series(stock, history, tick, ref_date, realtime=True):
    """Reuse validated history, but always overlay this round's live tick."""
    global g_reference_series_cache_date, g_reference_series_cache
    if g_reference_series_cache_date != ref_date:
        g_reference_series_cache_date = ref_date
        g_reference_series_cache = {}
    cached = g_reference_series_cache.get(stock)
    if cached is None or cached[0] is not history:
        prepared = build_reference_series(history, {}, ref_date, realtime=False)
        cached = (history, prepared)
        g_reference_series_cache[stock] = cached
    if not realtime:
        return cached[1]
    return build_reference_series(
        history, tick, ref_date, realtime=True, prepared=cached[1]
    )


def calculate_window_metrics(opens, highs, lows, closes):
    size = min(len(opens), len(highs), len(lows), len(closes))
    # 24 bars cover the 20-day window and every preceding MA/neighbor input.
    offset = max(0, size - 24)
    opens = [safe_float(value) for value in opens[offset:size]]
    highs = [safe_float(value) for value in highs[offset:size]]
    lows = [safe_float(value) for value in lows[offset:size]]
    closes = [safe_float(value) for value in closes[offset:size]]
    size = len(closes)
    start20 = max(0, size - 20)
    start10 = max(0, size - 10)

    yin_count_20 = sum(
        1 for index in range(start20, size) if closes[index] < opens[index]
    )
    high_gain_10d_count = 0
    low_above_ma5_count_10d = 0
    all_close_above_ma10_10d = 1
    yin_indexes = []
    yin_no_touch_count = 0
    yin_up_count = 0
    yin_cross_star_count = 0

    for index in range(start10, size):
        previous_close = closes[index - 1] if index >= 1 else 0.0
        max_gain = calc_gain(highs[index], previous_close)
        if max_gain >= 6.5:
            high_gain_10d_count += 1
        ma5 = sum(closes[index - 4:index + 1]) / 5.0 if index >= 4 else 0.0
        low_above = ma5 > 0 and lows[index] > ma5
        if low_above:
            low_above_ma5_count_10d += 1
        ma10 = sum(closes[index - 9:index + 1]) / 10.0 if index >= 9 else 0.0
        if ma10 <= 0 or not (closes[index] > ma10):
            all_close_above_ma10_10d = 0
        if closes[index] >= opens[index]:
            continue
        yin_indexes.append(index)
        if low_above:
            yin_no_touch_count += 1
        if is_cross_star(opens[index], closes[index], previous_close):
            yin_cross_star_count += 1
        body = opens[index] - closes[index]
        upper_shadow = highs[index] - opens[index]
        lower_shadow = closes[index] - lows[index]
        if (upper_shadow > body and upper_shadow > lower_shadow) or max_gain >= 6.5:
            yin_up_count += 1

    continuous_4_no_touch = 0
    lower_shadow_pct_sum_4d = 0.0
    if size >= 4:
        continuous_4_no_touch = 1
        for index in range(size - 4, size):
            if index < 4:
                continuous_4_no_touch = 0
                break
            ma5 = sum(closes[index - 4:index + 1]) / 5.0
            if not (lows[index] > ma5):
                continuous_4_no_touch = 0
                break
            if closes[index] <= 0:
                continuous_4_no_touch = 0
                break
            lower_shadow = max(0.0, min(opens[index], closes[index]) - lows[index])
            lower_shadow_pct_sum_4d += lower_shadow / closes[index] * 100.0

    yin_pull_prev_or_next = 0
    yin_pull_next = 0
    all_yin_eaten_next = 0
    if yin_indexes:
        yin_pull_prev_or_next = 1
        yin_pull_next = 1
        all_yin_eaten_next = 1
        end_index = size - 1
        for index in yin_indexes:
            previous_ok = False
            if index - 1 >= 1:
                previous_ok = calc_gain(highs[index - 1], closes[index - 2]) >= 6.5
            next_high_gain_ok = False
            next_eaten_ok = False
            if index + 1 <= end_index:
                next_high_gain_ok = (
                    calc_gain(highs[index + 1], closes[index]) >= 6.5
                )
                next_eaten_ok = closes[index + 1] > opens[index]
            if not (next_high_gain_ok or next_eaten_ok):
                yin_pull_next = 0
            if not next_eaten_ok:
                all_yin_eaten_next = 0
            if not (previous_ok or next_high_gain_ok):
                yin_pull_prev_or_next = 0

    yin_count_10d = len(yin_indexes)
    if yin_count_10d == 0:
        all_yin_no_touch = 0
        all_yin_up = 0
        all_yin_cross_star = 0
        is_quanyang_10d = 1
        yin_pull_prev_or_next = 0
        yin_pull_next = 1
        all_yin_eaten_next = 0
    else:
        all_yin_no_touch = int(yin_no_touch_count == yin_count_10d)
        all_yin_up = int(yin_up_count == yin_count_10d)
        all_yin_cross_star = int(yin_cross_star_count == yin_count_10d)
        is_quanyang_10d = 0

    return {
        "yin_count_20": int(yin_count_20),
        "yin_count_10d": int(yin_count_10d),
        "high_gain_10d_count": int(high_gain_10d_count),
        "low_above_ma5_count_10d": int(low_above_ma5_count_10d),
        "all_close_above_ma10_10d": int(all_close_above_ma10_10d),
        "all_yin_no_touch": int(all_yin_no_touch),
        "all_yin_up": int(all_yin_up),
        "all_yin_cross_star": int(all_yin_cross_star),
        "yin_pull_next": int(yin_pull_next),
        "yin_pull_prev_or_next": int(yin_pull_prev_or_next),
        "all_yin_eaten_next": int(all_yin_eaten_next),
        "continuous_4_no_touch": int(continuous_4_no_touch),
        "lower_shadow_pct_sum_4d": round(lower_shadow_pct_sum_4d, 6) if continuous_4_no_touch else 0.0,
        "is_quanyang_10d": int(is_quanyang_10d),
    }


def prepare_scan_window_metrics(series):
    """Cache only completed bars; the reference candle is never cached here."""
    opens, highs, lows, closes = (
        [safe_float(value) for value in series[field][-24:-1]]
        for field in ("opens", "highs", "lows", "closes")
    )
    high_gains = [0.0] * 13 + [
        calc_gain(highs[index], closes[index - 1]) for index in range(13, 23)
    ]
    ma5s = [0.0] * 14 + [
        sum(closes[index - 4:index + 1]) / 5.0 for index in range(14, 23)
    ]
    yin_indexes = [index for index in range(14, 23) if closes[index] < opens[index]]
    low_above = [
        lows[index] > ma5s[index] and ma5s[index] > 0 for index in range(14, 23)
    ]
    yin_up = []
    for index in yin_indexes:
        body = opens[index] - closes[index]
        upper = highs[index] - opens[index]
        lower = closes[index] - lows[index]
        yin_up.append((upper > body and upper > lower) or high_gains[index] >= 6.5)
    pull_next = eaten_next = pull_prev_or_next = True
    qy2_yin_ok = qy2_eaten_next = True
    for index in yin_indexes:
        if index == 22:
            continue
        next_high = high_gains[index + 1] >= 6.5
        eaten = closes[index + 1] > opens[index]
        pull_next = pull_next and (next_high or eaten)
        eaten_next = eaten_next and eaten
        pull_prev_or_next = pull_prev_or_next and (
            high_gains[index - 1] >= 6.5 or next_high
        )
        qy2_eaten_next = qy2_eaten_next and eaten
        low_gte_ma5 = ma5s[index] > 0 and lows[index] + 1e-9 >= ma5s[index]
        next_yang_eaten = eaten and closes[index + 1] > opens[index + 1]
        qy2_yin_ok = qy2_yin_ok and (low_gte_ma5 or next_yang_eaten)
    close_below_count = 0
    close_below_sum = 0.0
    for index in range(14, 23):
        if ma5s[index] > 0 and closes[index] < ma5s[index]:
            close_below_count += 1
            close_below_sum += (ma5s[index] - closes[index]) / ma5s[index] * 100.0
    continuous = all(
        lows[index] > ma5s[index] and closes[index] > 0 for index in range(20, 23)
    )
    shadow_sum = 0.0
    if continuous:
        for index in range(20, 23):
            lower = max(0.0, min(opens[index], closes[index]) - lows[index])
            shadow_sum += lower / closes[index] * 100.0
    return {
        "closes": closes,
        "last_open": opens[-1],
        "last_is_yin": closes[-1] < opens[-1],
        "last_high_gain": high_gains[-1],
        "previous_high_gain": high_gains[-2],
        "last_low_gte_ma5": ma5s[-1] > 0 and lows[-1] + 1e-9 >= ma5s[-1],
        "qy2_yin_ok": qy2_yin_ok,
        "qy2_eaten_next": qy2_eaten_next,
        "close_below_count": close_below_count,
        "close_below_sum": close_below_sum,
        "all_yin_no_touch": all(
            lows[index] > ma5s[index] and ma5s[index] > 0 for index in yin_indexes
        ),
        "all_yin_up": all(yin_up),
        "all_yin_cross_star": all(
            is_cross_star(opens[index], closes[index], closes[index - 1])
            for index in yin_indexes
        ),
        "yin_count_20": sum(closes[index] < opens[index] for index in range(4, 23)),
        "yin_count_10d": len(yin_indexes),
        "high_gain_10d_count": sum(value >= 6.5 for value in high_gains[14:]),
        "low_above_ma5_count_10d": sum(low_above),
        "all_close_above_ma10_10d": all(
            closes[index] > sum(closes[index - 9:index + 1]) / 10.0
            for index in range(14, 23)
        ),
        "yin_pull_next": pull_next,
        "all_yin_eaten_next": eaten_next,
        "yin_pull_prev_or_next": pull_prev_or_next,
        "continuous_4_no_touch": continuous,
        "shadow_sum": shadow_sum,
        "prev3_metrics": calculate_prev3_shadow_metrics(
            opens, highs, lows, closes, reference_index=23
        ),
        "prior_high_20d": max(highs[-19:]),
    }


def update_scan_window_metrics(prepared, series):
    open_price, high_price, low_price, close_price = (
        safe_float(series[field][-1]) for field in ("opens", "highs", "lows", "closes")
    )
    closes = prepared["closes"]
    high_gain = calc_gain(high_price, closes[-1])
    ma5 = sum(closes[-4:] + [close_price]) / 5.0
    ma10 = sum(closes[-9:] + [close_price]) / 10.0
    yin = close_price < open_price
    yin_count = prepared["yin_count_10d"] + int(yin)
    low_above = ma5 > 0 and low_price > ma5
    upper = high_price - open_price
    lower = close_price - low_price
    yin_up = (upper > open_price - close_price and upper > lower) or high_gain >= 6.5
    pull_next = prepared["yin_pull_next"]
    eaten_next = prepared["all_yin_eaten_next"]
    pull_prev_or_next = prepared["yin_pull_prev_or_next"]
    if prepared["last_is_yin"]:
        eaten = close_price > prepared["last_open"]
        pull_next = pull_next and (high_gain >= 6.5 or eaten)
        eaten_next = eaten_next and eaten
        previous_high = prepared["previous_high_gain"] >= 6.5
        pull_prev_or_next = pull_prev_or_next and (previous_high or high_gain >= 6.5)
    if yin:
        pull_next = eaten_next = False
        pull_prev_or_next = pull_prev_or_next and prepared["last_high_gain"] >= 6.5
    continuous = (
        prepared["continuous_4_no_touch"] and low_price > ma5 and close_price > 0
    )
    shadow_sum = prepared["shadow_sum"]
    if continuous:
        lower_shadow = max(0.0, min(open_price, close_price) - low_price)
        shadow_sum += lower_shadow / close_price * 100.0
    return {
        "yin_count_20": prepared["yin_count_20"] + int(yin),
        "yin_count_10d": yin_count,
        "high_gain_10d_count": prepared["high_gain_10d_count"] + int(high_gain >= 6.5),
        "low_above_ma5_count_10d": prepared["low_above_ma5_count_10d"] + int(low_above),
        "all_close_above_ma10_10d": int(
            prepared["all_close_above_ma10_10d"] and ma10 > 0 and close_price > ma10
        ),
        "all_yin_no_touch": int(
            yin_count > 0 and prepared["all_yin_no_touch"] and (not yin or low_above)
        ),
        "all_yin_up": int(
            yin_count > 0 and prepared["all_yin_up"] and (not yin or yin_up)
        ),
        "all_yin_cross_star": int(
            yin_count > 0 and prepared["all_yin_cross_star"]
            and (not yin or is_cross_star(open_price, close_price, closes[-1]))
        ),
        "yin_pull_next": int(pull_next) if yin_count else 1,
        "yin_pull_prev_or_next": int(yin_count > 0 and pull_prev_or_next),
        "all_yin_eaten_next": int(yin_count > 0 and eaten_next),
        "continuous_4_no_touch": int(continuous),
        "lower_shadow_pct_sum_4d": round(shadow_sum, 6) if continuous else 0.0,
        "is_quanyang_10d": int(yin_count == 0),
    }


def calculate_scan_window_metrics(stock, history, series, ref_date, shared=None):
    global g_window_metrics_cache_date, g_window_metrics_cache
    if g_window_metrics_cache_date != ref_date:
        g_window_metrics_cache_date = ref_date
        g_window_metrics_cache = {}
    lengths = [
        len(series.get(field) or []) for field in ("opens", "highs", "lows", "closes")
    ]
    if min(lengths) < 24 or len(set(lengths)) != 1:
        return calculate_window_metrics(
            series["opens"], series["highs"], series["lows"], series["closes"]
        )
    source = g_reference_series_cache.get(stock)
    cached = g_window_metrics_cache.get(stock)
    if (
        cached is None or cached[0] is not history
        or cached[1] != lengths[0] or cached[2] is not source
    ):
        cached = (history, lengths[0], source, prepare_scan_window_metrics(series))
        g_window_metrics_cache[stock] = cached
    prepared = cached[3]
    if shared is not None:
        shared["scan_window"] = prepared
        shared["prev3_metrics"] = prepared["prev3_metrics"]
        shared["prior_high_20d"] = prepared["prior_high_20d"]
    return update_scan_window_metrics(prepared, series)


def calculate_scan_qy2_metrics(prepared, opens, lows, closes):
    if prepared is None:
        return calculate_qy2_metrics(opens, lows, closes)
    open_price = safe_float(opens[-1])
    close_price = safe_float(closes[-1])
    low_price = safe_float(lows[-1])
    ma5 = sum(prepared["closes"][-4:] + [close_price]) / 5.0
    yin_ok = prepared["qy2_yin_ok"]
    eaten_next = prepared["qy2_eaten_next"]
    if prepared["last_is_yin"]:
        eaten = close_price > prepared["last_open"]
        eaten_next = eaten_next and eaten
        yin_ok = yin_ok and (
            prepared["last_low_gte_ma5"] or (eaten and close_price > open_price)
        )
    if close_price < open_price:
        eaten_next = False
        yin_ok = yin_ok and ma5 > 0 and low_price + 1e-9 >= ma5
    below = ma5 > 0 and close_price < ma5
    below_sum = prepared["close_below_sum"]
    if below:
        below_sum += (ma5 - close_price) / ma5 * 100.0
    return {
        "all_yin_low_gte_ma5_or_eaten_10d": int(yin_ok),
        "all_yin_eaten_next_10d": int(eaten_next),
        "close_below_ma5_count_10d": prepared["close_below_count"] + int(below),
        "close_below_ma5_abs_pct_sum_10d": round(below_sum, 6),
    }


def shared_series_value(shared, name, calculate):
    if shared is None:
        return calculate()
    if name not in shared:
        shared[name] = calculate()
    return shared[name]


def calculate_qy1_metrics(opens, highs, lows, closes):
    size = min(len(opens), len(highs), len(lows), len(closes))
    offset = max(0, size - 24)
    opens = [safe_float(value) for value in opens[offset:size]]
    highs = [safe_float(value) for value in highs[offset:size]]
    lows = [safe_float(value) for value in lows[offset:size]]
    closes = [safe_float(value) for value in closes[offset:size]]
    size = len(closes)
    if size < 10:
        return {
            "all_yin_no_touch_or_eaten_10d": 0,
            "strong_k_count_10d": 0,
            "close_below_ma5_count_10d": 0,
            "close_below_ma5_abs_pct_sum_10d": 0.0,
        }

    start10 = size - 10
    end_index = size - 1
    all_yin_ok = 1
    strong_k_count = 0
    close_below_ma5_count = 0
    close_below_ma5_abs_pct_sum = 0.0
    for index in range(start10, size):
        previous_close = closes[index - 1] if index >= 1 else 0.0
        ma5 = sum(closes[index - 4:index + 1]) / 5.0 if index >= 4 else 0.0
        low_above_ma5 = ma5 > 0 and lows[index] > ma5
        if ma5 > 0 and closes[index] < ma5:
            close_below_ma5_count += 1
            close_below_ma5_abs_pct_sum += (
                (ma5 - closes[index]) / ma5 * 100.0
            )

        is_yang = closes[index] > opens[index]
        body = closes[index] - opens[index] if is_yang else 0.0
        upper_shadow = max(0.0, highs[index] - closes[index])
        lower_shadow = max(0.0, opens[index] - lows[index])
        high_gain_yang_short_shadows = (
            is_yang
            and calc_gain(highs[index], previous_close) >= HIGH_GAIN_THRESHOLD_PCT
            and upper_shadow <= body + 1e-9
            and lower_shadow <= body + 1e-9
        )
        if high_gain_yang_short_shadows or low_above_ma5:
            strong_k_count += 1

        if closes[index] < opens[index] and not low_above_ma5:
            next_index = index + 1
            eaten_by_next_yang = (
                next_index <= end_index
                and closes[next_index] > opens[next_index]
                and closes[next_index] > opens[index]
            )
            if not eaten_by_next_yang:
                all_yin_ok = 0

    return {
        "all_yin_no_touch_or_eaten_10d": int(all_yin_ok),
        "strong_k_count_10d": int(strong_k_count),
        "close_below_ma5_count_10d": int(close_below_ma5_count),
        "close_below_ma5_abs_pct_sum_10d": round(
            close_below_ma5_abs_pct_sum, 6
        ),
    }


def calculate_qy2_metrics(opens, lows, closes):
    size = min(len(opens), len(lows), len(closes))
    if size < 10:
        return {
            "all_yin_low_gte_ma5_or_eaten_10d": 0,
            "all_yin_eaten_next_10d": 0,
            "close_below_ma5_count_10d": 0,
            "close_below_ma5_abs_pct_sum_10d": 0.0,
        }
    all_yin_ok = 1
    all_yin_eaten_next = 1
    close_below_ma5_count = 0
    close_below_ma5_abs_pct_sum = 0.0
    for index in range(size - 10, size):
        yin_open = safe_float(opens[index])
        close_value = safe_float(closes[index])
        ma5 = sum(safe_float(value) for value in closes[index - 4:index + 1]) / 5.0
        if ma5 > 0 and close_value < ma5:
            close_below_ma5_count += 1
            close_below_ma5_abs_pct_sum += (ma5 - close_value) / ma5 * 100.0
        if close_value >= yin_open:
            continue
        next_close = safe_float(closes[index + 1]) if index + 1 < size else 0.0
        eaten_next = next_close > yin_open
        if not eaten_next:
            all_yin_eaten_next = 0
        low_not_below_ma5 = ma5 > 0 and safe_float(lows[index]) + 1e-9 >= ma5
        eaten_by_next_yang = (
            eaten_next and next_close > safe_float(opens[index + 1])
        )
        if not (low_not_below_ma5 or eaten_by_next_yang):
            all_yin_ok = 0
    return {
        "all_yin_low_gte_ma5_or_eaten_10d": all_yin_ok,
        "all_yin_eaten_next_10d": all_yin_eaten_next,
        "close_below_ma5_count_10d": close_below_ma5_count,
        "close_below_ma5_abs_pct_sum_10d": round(
            close_below_ma5_abs_pct_sum, 6
        ),
    }


def is_qy2_history_candidate(history, ref_date):
    history = history if isinstance(history, dict) else {}
    ref_date = normalize_trade_date(ref_date)
    if not ref_date:
        return False
    dates = history.get("dates") or []
    opens = history.get("opens") or []
    lows = history.get("lows") or []
    closes = history.get("closes") or []
    size = min(len(dates), len(opens), len(lows), len(closes))
    prior_rows = []
    for index in range(size):
        date_text = normalize_trade_date(dates[index])
        if not date_text or date_text >= ref_date:
            continue
        opened = safe_float(opens[index])
        low = safe_float(lows[index])
        closed = safe_float(closes[index])
        if min(opened, low, closed) > 0:
            prior_rows.append((opened, low, closed))
    if len(prior_rows) < 19:
        return False
    first_index = len(prior_rows) - 9
    for index in range(first_index, len(prior_rows) - 1):
        opened, low, closed = prior_rows[index]
        if closed >= opened:
            continue
        ma5 = sum(row[2] for row in prior_rows[index - 4:index + 1]) / 5.0
        if low + 1e-9 >= ma5:
            continue
        next_open, _next_low, next_close = prior_rows[index + 1]
        if not (next_close > next_open and next_close > opened):
            return False
    return True


def ensure_qy2_candidate_cache(ref_date):
    global g_qy2_candidate_cache_date, g_qy2_candidate_stocks
    ref_date = normalize_trade_date(ref_date)
    if g_qy2_candidate_cache_date == ref_date:
        return g_qy2_candidate_stocks
    started = time.time()
    candidates = {
        stock
        for stock, history in g_history_data.items()
        if is_qy2_history_candidate(history, ref_date)
    }
    g_qy2_candidate_cache_date = ref_date
    g_qy2_candidate_stocks = candidates
    log(
        "[QY2-CACHE] date=%s candidates=%d market=%d seconds=%.3f"
        % (ref_date, len(candidates), len(g_stock_pool), time.time() - started),
        date_text=ref_date,
    )
    return g_qy2_candidate_stocks


def is_pz1_history_candidate(history, ref_date, stock_code=""):
    """Keep prior windows that can satisfy any of the eight PZ1 entry routes."""
    history = history if isinstance(history, dict) else {}
    ref_date = normalize_trade_date(ref_date)
    if not ref_date:
        return False
    dates = list(history.get("dates") or [])
    opens = list(history.get("opens") or [])
    highs = list(history.get("highs") or [])
    lows = list(history.get("lows") or [])
    closes = list(history.get("closes") or [])
    size = min(len(dates), len(opens), len(highs), len(lows), len(closes))
    prior_rows = []
    for index in range(size):
        date_text = normalize_trade_date(dates[index])
        if not date_text or date_text >= ref_date:
            continue
        open_price = safe_float(opens[index])
        high_price = safe_float(highs[index])
        low_price = safe_float(lows[index])
        close_price = safe_float(closes[index])
        if min(open_price, high_price, low_price, close_price) <= 0:
            continue
        prior_rows.append((open_price, high_price, low_price, close_price))
    if len(prior_rows) < 19:
        return False
    prior_9 = prior_rows[-9:]
    if sum(row[3] < row[0] for row in prior_rows[-19:]) <= PZ1_MAX_YIN_COUNT_20:
        return True
    if all(row[3] >= row[0] for row in prior_9):
        return True
    prior_10 = prior_rows[-10:]
    if sum(
        calc_gain(prior_10[index][1], prior_10[index - 1][3]) >= HIGH_GAIN_THRESHOLD_PCT
        for index in range(1, len(prior_10))
    ) >= MIN_HIGH_GAIN_COUNT_10D - 1:
        return True
    shadow_metrics = calculate_prev3_shadow_metrics(
        [row[0] for row in prior_rows],
        [row[1] for row in prior_rows],
        [row[2] for row in prior_rows],
        [row[3] for row in prior_rows],
        reference_index=len(prior_rows),
    )
    if shadow_metrics["prev3_short_shadow_met"] == 1:
        return True
    if stock_code:
        # Keep a possible limit-up-gap match before the reference quote exists.
        # Use the same exact-cent rounding as the final marker, not its gain gate.
        possible_prior = prior_rows[-30:]
        previous_close = possible_prior[-1][3]
        multiplier = Decimal("1") + Decimal(str(get_limit_up_ratio(stock_code))) / Decimal("100")
        possible_close = float((Decimal(str(previous_close)) * multiplier).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ))
        possible_gap = calculate_limit_up_gap_flags(stock_code, {
            "opens": [row[0] for row in possible_prior] + [previous_close],
            "closes": [row[3] for row in possible_prior] + [possible_close],
            "pre_close": previous_close,
        })
        if possible_gap["limit_up_gap_yin_lte1_30d"] == 1:
            return True
    # Both longer-window and five-pull one-yin patterns imply the last-10 pattern.
    # Yesterday's yin is left pending until this round's reference candle exists.
    for index in range(len(prior_9) - 1):
        day_open, _high, _low, day_close = prior_9[index]
        if day_close >= day_open:
            continue
        next_open, _next_high, _next_low, next_close = prior_9[index + 1]
        if next_close < next_open:
            return False
        if stock_code and is_limit_down_by_price(next_close, day_close, stock_code):
            return False
    return True


def ensure_pz1_candidate_cache(ref_date):
    global g_pz1_candidate_cache_date, g_pz1_candidate_stocks
    ref_date = normalize_trade_date(ref_date)
    if g_pz1_candidate_cache_date == ref_date:
        return g_pz1_candidate_stocks
    started = time.time()
    candidates = {
        stock
        for stock, history in g_history_data.items()
        if is_pz1_history_candidate(history, ref_date, stock)
    }
    g_pz1_candidate_cache_date = ref_date
    g_pz1_candidate_stocks = candidates
    log(
        "[PZ1-CACHE] date=%s candidates=%d market=%d seconds=%.3f"
        % (ref_date, len(candidates), len(g_stock_pool), time.time() - started),
        date_text=ref_date,
    )
    return g_pz1_candidate_stocks


# ==================== general helpers ====================

def now_text(now=None):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def is_trading_session(current_time):
    if current_time.weekday() >= 5:
        return False
    current_hhmm = current_time.hour * 100 + current_time.minute
    return (925 <= current_hhmm <= 1130) or (1300 <= current_hhmm <= 1505)


def should_run_initial_scan(current_time=None, historical_mode=False):
    if historical_mode:
        return True
    if not CHECK_TRADING_TIME:
        return True
    return is_trading_session(current_time or datetime.now())


def simple_code(value):
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def is_risk_stock_name(name):
    text = str(name or "").strip()
    upper = text.upper()
    return bool(text) and ("\u9000" in text or "\u9000\u5e02" in text or "ST" in upper)


def get_log_dir(date_text=None):
    date_text = normalize_trade_date(date_text) or datetime.now().strftime("%Y%m%d")
    path = os.path.join(LOG_BASE_DIR, date_text, STRATEGY_DIR_NAME)
    if not os.path.exists(path):
        try:
            os.makedirs(path)
        except Exception:
            pass
    return path


def log(message, level="INFO", date_text=None):
    if not LOG_ENABLED:
        return
    line = "[%s] [%s] %s" % (now_text(), level, message)
    print(line)
    try:
        path = os.path.join(get_log_dir(date_text), "\u8fd0\u884c\u65e5\u5fd7.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def encode_payload(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def redis_key(prefix, ref_date):
    return "%s:%s" % (prefix, normalize_trade_date(ref_date))


def extract_stock_data(data, stock):
    stock_data = None
    if hasattr(data, "minor_xs"):
        try:
            stock_data = data.minor_xs(stock)
        except Exception:
            pass
    if stock_data is None and hasattr(data, "loc"):
        try:
            stock_data = data.loc[:, :, stock]
        except Exception:
            pass
    if stock_data is None:
        try:
            stock_data = data[stock]
        except Exception:
            pass
    return stock_data


def frame_values(frame, field, size):
    try:
        series = frame[field]
        return [safe_float(series.iloc[index]) for index in range(size)]
    except Exception:
        return [0.0] * size


def history_from_frame(frame, requested_end=""):
    if frame is None or len(frame) == 0:
        return None
    rows = []
    previous_close = 0.0
    for index in range(len(frame)):
        try:
            raw_date = frame.index[index]
            if hasattr(raw_date, "strftime"):
                date_text = raw_date.strftime("%Y%m%d")
            else:
                date_text = normalize_trade_date(raw_date)
            open_price = safe_float(frame["open"].iloc[index])
            high_price = safe_float(frame["high"].iloc[index])
            low_price = safe_float(frame["low"].iloc[index])
            close_price = safe_float(frame["close"].iloc[index])
        except Exception:
            continue
        if not date_text or (requested_end and date_text > requested_end):
            continue
        if min(open_price, high_price, low_price, close_price) <= 0:
            continue
        if (
            previous_close > 0
            and open_price == high_price == low_price == close_price
            and abs(close_price - previous_close) < 0.01
        ):
            continue
        volume = 0.0
        amount = 0.0
        try:
            volume = safe_float(frame["volume"].iloc[index])
        except Exception:
            pass
        try:
            amount = safe_float(frame["amount"].iloc[index])
        except Exception:
            pass
        rows.append((date_text, open_price, high_price, low_price, close_price, volume, amount))
        previous_close = close_price
    if len(rows) < 25:
        return None
    rows = rows[-HISTORY_REQUEST_DAYS:]
    return {
        "dates": [row[0] for row in rows],
        "opens": [row[1] for row in rows],
        "highs": [row[2] for row in rows],
        "lows": [row[3] for row in rows],
        "closes": [row[4] for row in rows],
        "volumes": [row[5] for row in rows],
        "amounts": [row[6] for row in rows],
    }


def preload_history_data(ContextInfo, end_date=""):
    global g_history_data, g_history_reload_date
    global g_pz1_candidate_cache_date, g_pz1_candidate_stocks
    global g_qy2_candidate_cache_date, g_qy2_candidate_stocks
    global g_reference_series_cache_date, g_reference_series_cache
    global g_window_metrics_cache_date, g_window_metrics_cache
    requested_end = normalize_trade_date(end_date)
    if requested_end:
        query_end = (datetime.strptime(requested_end, "%Y%m%d") + timedelta(days=7)).strftime("%Y%m%d")
    else:
        query_end = (datetime.now() + timedelta(days=7)).strftime("%Y%m%d")
    loaded = {}
    failed = 0
    failed_codes = []
    total = len(g_stock_pool)
    started = time.time()
    for offset in range(0, total, HISTORY_BATCH_SIZE):
        batch = g_stock_pool[offset:offset + HISTORY_BATCH_SIZE]
        try:
            try:
                data = ContextInfo.get_market_data_ex(
                    ["open", "high", "low", "close", "volume", "amount"],
                    batch,
                    period="1d",
                    end_time=query_end,
                    count=HISTORY_REQUEST_DAYS,
                    dividend_type="front",
                )
            except Exception:
                data = ContextInfo.get_market_data_ex(
                    ["open", "high", "low", "close"],
                    batch,
                    period="1d",
                    end_time=query_end,
                    count=HISTORY_REQUEST_DAYS,
                    dividend_type="front",
                )
        except Exception as error:
            log("[HISTORY] batch failed: %s" % error, "ERROR", requested_end)
            failed += len(batch)
            failed_codes.extend(batch[:max(0, 20 - len(failed_codes))])
            continue
        for stock in batch:
            try:
                history = history_from_frame(extract_stock_data(data, stock), requested_end)
                if history:
                    loaded[stock] = history
                else:
                    failed += 1
                    if len(failed_codes) < 20:
                        failed_codes.append(stock)
            except Exception:
                failed += 1
                if len(failed_codes) < 20:
                    failed_codes.append(stock)
        log(
            "[HISTORY] progress=%d/%d loaded=%d failed=%d"
            % (min(offset + len(batch), total), total, len(loaded), failed),
            date_text=requested_end,
        )
    if loaded:
        g_history_data = loaded
        g_pz1_candidate_cache_date = ""
        g_pz1_candidate_stocks = set()
        g_qy2_candidate_cache_date = ""
        g_qy2_candidate_stocks = set()
        g_reference_series_cache_date = ""
        g_reference_series_cache = {}
        g_window_metrics_cache_date = ""
        g_window_metrics_cache = {}
    latest_dates = [
        history.get("dates", [])[-1]
        for history in loaded.values()
        if history.get("dates")
    ]
    latest_loaded_date = max(latest_dates) if latest_dates else ""
    g_history_reload_date = datetime.now().strftime("%Y%m%d")
    log(
        "[HISTORY] completed loaded=%d failed=%d seconds=%.3f"
        % (len(loaded), failed, time.time() - started),
        date_text=requested_end,
    )
    log(
        "[HISTORY] latest_loaded_date=%s"
        % (latest_loaded_date or "missing"),
        date_text=requested_end,
    )
    if failed_codes:
        log(
            "[HISTORY] failed sample=%s%s"
            % (
                ",".join(failed_codes),
                " ..." if failed > len(failed_codes) else "",
            ),
            "WARN",
            requested_end,
        )
    return len(loaded), failed


def latest_history_date():
    latest = ""
    for history in g_history_data.values():
        dates = history.get("dates") or []
        if dates and dates[-1] > latest:
            latest = dates[-1]
    return latest


def resolve_reference_date(current_time=None):
    configured = normalize_trade_date(REFERENCE_DATE)
    if configured:
        return configured
    current_time = current_time or datetime.now()
    today = current_time.strftime("%Y%m%d")
    hhmm = current_time.hour * 100 + current_time.minute
    if current_time.weekday() < 5 and hhmm >= 925:
        return today
    return latest_history_date() or today


def synthetic_tick_from_series(series):
    closes = series.get("closes") or []
    if not closes:
        return {}
    return {
        "open": series["opens"][-1],
        "high": series["highs"][-1],
        "low": series["lows"][-1],
        "lastPrice": closes[-1],
        "price": closes[-1],
        "preClose": series.get("pre_close") or (closes[-2] if len(closes) >= 2 else 0.0),
        "lastClose": series.get("pre_close") or (closes[-2] if len(closes) >= 2 else 0.0),
    }


def build_kline_row(stock, trade_date, series, index):
    close_price = safe_float(series["closes"][index])
    pre_close = safe_float(series["closes"][index - 1]) if index >= 1 else 0.0
    if trade_date == series.get("_ref_date") and safe_float(series.get("pre_close")) > 0:
        pre_close = safe_float(series["pre_close"])
    open_price = safe_float(series["opens"][index])
    high_price = max(safe_float(series["highs"][index]), open_price, close_price)
    low_price = min(safe_float(series["lows"][index]), open_price, close_price)
    if min(open_price, high_price, low_price, close_price) <= 0:
        return None
    return {
        "ts_code": stock,
        "stock_code_with_market": stock,
        "trade_date": normalize_trade_date(trade_date),
        "open": round(open_price, 4),
        "high": round(high_price, 4),
        "low": round(low_price, 4),
        "close": round(close_price, 4),
        "pre_close": round(pre_close, 4),
        "pct_chg": round(calc_gain(close_price, pre_close), 4),
        "vol": round(safe_float(series.get("volumes", [0.0] * len(series["closes"]))[index]), 4),
        "amount": round(safe_float(series.get("amounts", [0.0] * len(series["closes"]))[index]), 4),
    }


def build_kline_payloads(stock, name, series, ref_date, snapshot_at,
                         published_codes=None, payload_cache=None):
    code = simple_code(stock)
    include_history = published_codes is None or code not in published_codes
    cached = payload_cache.get(stock) if payload_cache is not None else None
    if cached is not None and (not include_history or cached[0] is not None):
        return cached
    rows = []
    indexed_dates = list(enumerate(series.get("dates") or []))
    if not include_history:
        indexed_dates.sort(key=lambda item: (item[1], item[0]), reverse=True)
    for index, date_text in indexed_dates:
        if not include_history and date_text > ref_date:
            continue
        row = build_kline_row(stock, date_text, series, index)
        if row and row["trade_date"] <= ref_date:
            rows.append(row)
            if not include_history:
                break
    rows.sort(key=lambda item: item["trade_date"])
    reference_row = None
    history_rows = []
    for row in rows:
        if row["trade_date"] == ref_date:
            reference_row = row
        elif row["trade_date"] < ref_date:
            history_rows.append(row)
    if reference_row is None and rows:
        reference_row = rows[-1]
        history_rows = rows[:-1]
    history_rows = history_rows[-KLINE_HISTORY_DAYS:]
    published_at = now_text()
    history_payload = {
        "code": simple_code(stock),
        "name": name,
        "stock_code_with_market": stock,
        "updated_at": published_at,
        "rows": history_rows,
    } if include_history else None
    realtime_payload = {
        "code": simple_code(stock),
        "name": name,
        "stock_code_with_market": stock,
        "snapshot_at": snapshot_at,
        "published_at": published_at,
        "updated_at": published_at,
        "row": reference_row,
    }
    result = (history_payload, realtime_payload)
    if payload_cache is not None:
        payload_cache[stock] = result
    return result


# ==================== Redis publishing ====================

def init_redis():
    global g_redis_client
    if redis is None:
        log("[REDIS] redis module missing", "ERROR")
        return False
    try:
        g_redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        g_redis_client.ping()
        log("[REDIS] connected %s:%s" % (REDIS_HOST, REDIS_PORT))
        return True
    except Exception as error:
        log("[REDIS] connection failed: %s" % error, "ERROR")
        g_redis_client = None
        return False


def write_status(ref_date, values, pipeline=None):
    if g_redis_client is None:
        return
    mapping = {}
    for key, value in values.items():
        if isinstance(value, (dict, list, tuple)):
            mapping[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            mapping[key] = str(value)
    key = redis_key(STATUS_KEY_PREFIX, ref_date)
    pipe = pipeline if pipeline is not None else g_redis_client.pipeline(transaction=False)
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, REDIS_TTL_SEC)
    if pipeline is None:
        pipe.execute()


def publish_performance_record(ref_date, values):
    if g_redis_client is None or not ref_date:
        return
    record = dict(values or {})
    record["recorded_at"] = now_text()
    key = redis_key(PERFORMANCE_KEY_PREFIX, ref_date)
    pipe = g_redis_client.pipeline(transaction=False)
    pipe.lpush(key, encode_payload(record))
    pipe.expire(key, REDIS_TTL_SEC)
    pipe.execute()


def publish_full_hash(full_key, mapping):
    started = time.time()
    temp_key = "%s:tmp:%s" % (full_key, uuid.uuid4().hex)
    encoded = {field: encode_payload(value) for field, value in mapping.items()}
    # Keep the full snapshot replacement atomic in one network round trip.
    pipe = g_redis_client.pipeline(transaction=True)
    if encoded:
        pipe.hset(temp_key, mapping=encoded)
    pipe.expire(temp_key, REDIS_TTL_SEC)
    pipe.rename(temp_key, full_key)
    pipe.execute()
    return time.time() - started


def publish_pz_snapshot(ref_date, results, scan_info):
    mapping = {result["code"]: result for result in results}
    mapping["_scan_info"] = scan_info
    return publish_full_hash(redis_key(REDIS_KEY_PREFIX, ref_date), mapping)


def publish_pz1_snapshot(ref_date, results, scan_info):
    mapping = {result["code"]: result for result in results}
    mapping["_scan_info"] = scan_info
    return publish_full_hash(redis_key(PZ1_REDIS_KEY_PREFIX, ref_date), mapping)


def publish_qy1_snapshot(ref_date, results, scan_info):
    mapping = {result["code"]: result for result in results}
    mapping["_scan_info"] = scan_info
    return publish_full_hash(redis_key(QY1_REDIS_KEY_PREFIX, ref_date), mapping)


def publish_qy2_snapshot(ref_date, results, scan_info):
    mapping = {result["code"]: result for result in results}
    mapping["_scan_info"] = scan_info
    return publish_full_hash(redis_key(QY2_REDIS_KEY_PREFIX, ref_date), mapping)


def ensure_history_publish_state(ref_date):
    global g_history_published_date, g_history_published_codes
    if g_history_published_date == ref_date:
        return
    g_history_published_date = ref_date
    g_history_published_codes = set()
    if g_redis_client is None:
        return
    try:
        values = g_redis_client.hkeys(redis_key(KLINE_HISTORY_PREFIX, ref_date))
        g_history_published_codes = {
            simple_code(value) for value in values if not str(value).startswith("_")
        }
    except Exception as error:
        log("[KLINE] history state load failed: %s" % error, "WARN", ref_date)


def publish_kline_history(ref_date, payloads):
    ensure_history_publish_state(ref_date)
    started = time.time()
    new_payloads = {
        code: payload
        for code, payload in payloads.items()
        if code not in g_history_published_codes
    }
    if not new_payloads:
        return 0.0, 0
    key = redis_key(KLINE_HISTORY_PREFIX, ref_date)
    mapping = {code: encode_payload(payload) for code, payload in new_payloads.items()}
    mapping["_kline_info"] = encode_payload({
        "updated_at": now_text(),
        "new_count": len(new_payloads),
        "history_days": KLINE_HISTORY_DAYS,
    })
    pipe = g_redis_client.pipeline(transaction=False)
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, REDIS_TTL_SEC)
    pipe.execute()
    g_history_published_codes.update(new_payloads)
    return time.time() - started, len(new_payloads)


def publish_kline_realtime(ref_date, payloads, snapshot_at):
    mapping = dict(payloads)
    mapping["_kline_info"] = {
        "snapshot_at": snapshot_at,
        "published_at": now_text(),
        "count": len(payloads),
    }
    return publish_full_hash(redis_key(KLINE_REALTIME_PREFIX, ref_date), mapping)


def ensure_qy1_history_publish_state(ref_date):
    global g_qy1_history_published_date, g_qy1_history_published_codes
    if g_qy1_history_published_date == ref_date:
        return
    g_qy1_history_published_date = ref_date
    g_qy1_history_published_codes = set()
    if g_redis_client is None:
        return
    try:
        values = g_redis_client.hkeys(redis_key(QY1_KLINE_HISTORY_PREFIX, ref_date))
        g_qy1_history_published_codes = {
            simple_code(value) for value in values if not str(value).startswith("_")
        }
    except Exception as error:
        log("[QY1-KLINE] history state load failed: %s" % error, "WARN", ref_date)


def publish_qy1_kline_history(ref_date, payloads):
    ensure_qy1_history_publish_state(ref_date)
    started = time.time()
    new_payloads = {
        code: payload
        for code, payload in payloads.items()
        if code not in g_qy1_history_published_codes
    }
    if not new_payloads:
        return 0.0, 0
    key = redis_key(QY1_KLINE_HISTORY_PREFIX, ref_date)
    mapping = {code: encode_payload(payload) for code, payload in new_payloads.items()}
    mapping["_kline_info"] = encode_payload({
        "updated_at": now_text(),
        "new_count": len(new_payloads),
        "history_days": KLINE_HISTORY_DAYS,
    })
    pipe = g_redis_client.pipeline(transaction=False)
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, REDIS_TTL_SEC)
    pipe.execute()
    g_qy1_history_published_codes.update(new_payloads)
    return time.time() - started, len(new_payloads)


def publish_qy1_kline_realtime(ref_date, payloads, snapshot_at):
    mapping = dict(payloads)
    mapping["_kline_info"] = {
        "snapshot_at": snapshot_at,
        "published_at": now_text(),
        "count": len(payloads),
    }
    return publish_full_hash(redis_key(QY1_KLINE_REALTIME_PREFIX, ref_date), mapping)


def ensure_qy2_history_publish_state(ref_date):
    global g_qy2_history_published_date, g_qy2_history_published_codes
    if g_qy2_history_published_date == ref_date:
        return
    g_qy2_history_published_date = ref_date
    g_qy2_history_published_codes = set()
    if g_redis_client is None:
        return
    try:
        values = g_redis_client.hkeys(redis_key(QY2_KLINE_HISTORY_PREFIX, ref_date))
        g_qy2_history_published_codes = {
            simple_code(value) for value in values if not str(value).startswith("_")
        }
    except Exception as error:
        log("[QY2-KLINE] history state load failed: %s" % error, "WARN", ref_date)


def publish_qy2_kline_history(ref_date, payloads):
    ensure_qy2_history_publish_state(ref_date)
    started = time.time()
    new_payloads = {
        code: payload
        for code, payload in payloads.items()
        if code not in g_qy2_history_published_codes
    }
    if not new_payloads:
        return 0.0, 0
    key = redis_key(QY2_KLINE_HISTORY_PREFIX, ref_date)
    mapping = {code: encode_payload(payload) for code, payload in new_payloads.items()}
    mapping["_kline_info"] = encode_payload({
        "updated_at": now_text(),
        "new_count": len(new_payloads),
        "history_days": KLINE_HISTORY_DAYS,
    })
    pipe = g_redis_client.pipeline(transaction=False)
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, REDIS_TTL_SEC)
    pipe.execute()
    g_qy2_history_published_codes.update(new_payloads)
    return time.time() - started, len(new_payloads)


def publish_qy2_kline_realtime(ref_date, payloads, snapshot_at):
    mapping = dict(payloads)
    mapping["_kline_info"] = {
        "snapshot_at": snapshot_at,
        "published_at": now_text(),
        "count": len(payloads),
    }
    return publish_full_hash(redis_key(QY2_KLINE_REALTIME_PREFIX, ref_date), mapping)


# ==================== DingTalk ====================

def dingtalk_dedup_key(ref_date, route=DINGTALK_ROUTE_YIN20):
    prefix = DINGTALK_DEDUP_PREFIX
    if route == DINGTALK_ROUTE_CONDITION3:
        prefix = DINGTALK_CONDITION3_DEDUP_PREFIX
    return redis_key(prefix, ref_date)


def dingtalk_task_token(route, code):
    return "%s:%s" % (route or DINGTALK_ROUTE_YIN20, simple_code(code))


def dingtalk_webhook_for_route(route):
    if route == DINGTALK_ROUTE_CONDITION3:
        return DINGTALK_CONDITION3_WEBHOOK
    return DINGTALK_WEBHOOK


def dingtalk_record_key(ref_date):
    return redis_key(DINGTALK_RECORD_PREFIX, ref_date)


def build_dingtalk_record(task, dingtalk_sent_at):
    result = task.get("result") or {}
    code = simple_code(task.get("code") or result.get("code"))
    ref_date = normalize_trade_date(task.get("ref_date"))
    if not code or not ref_date:
        return "", "", None
    record = {
        "code": code,
        "name": result.get("name", ""),
        "ref_date": ref_date,
        "selected_at": task.get("selected_at", ""),
        "dingtalk_sent_at": dingtalk_sent_at,
        "ref_gain": safe_float(result.get("ref_gain")),
        "yin_count_20": int(result.get("yin_count_20", 0) or 0),
        "yin_count_10d": int(result.get("yin_count_10d", 0) or 0),
        "is_quanyang_10d": int(result.get("is_quanyang_10d", 0) or 0),
        "qiangchi": int(result.get("qiangchi", 0) or 0),
        "condition_3_met": int(result.get("condition_3_met", 0) or 0),
        "five_pull_all_yin_up": int(
            result.get("five_pull_all_yin_up", 0) or 0
        ),
        "five_pull_all_yin_eaten_next": int(
            result.get("five_pull_all_yin_eaten_next", 0) or 0
        ),
        "five_pull_yin_pull_next": int(
            result.get("five_pull_yin_pull_next", 0) or 0
        ),
        "five_pull_all_yin_cross_star": int(
            result.get("five_pull_all_yin_cross_star", 0) or 0
        ),
        "five_pull_all_yin_no_touch": int(
            result.get("five_pull_all_yin_no_touch", 0) or 0
        ),
        "match_reasons": list(result.get("match_reasons") or []),
    }
    return code, ref_date, record


def persist_dingtalk_record(task, dingtalk_sent_at):
    if g_redis_client is None:
        return False
    code, ref_date, record = build_dingtalk_record(task, dingtalk_sent_at)
    if not code or not ref_date or record is None:
        return False
    try:
        key = dingtalk_record_key(ref_date)
        pipe = g_redis_client.pipeline(transaction=False)
        pipe.hset(key, mapping={code: encode_payload(record)})
        pipe.expire(key, REDIS_TTL_SEC)
        pipe.execute()
        return True
    except Exception as error:
        log(
            "[DINGTALK] record failed code=%s error=%s" % (code, error),
            "WARN",
            ref_date,
        )
        return False


def persist_dingtalk_success_once(task, dingtalk_sent_at):
    if g_redis_client is None:
        return False
    code, ref_date, record = build_dingtalk_record(task, dingtalk_sent_at)
    if not code or not ref_date or record is None:
        return False
    route = task.get("route", DINGTALK_ROUTE_YIN20)
    dedup_key = dingtalk_dedup_key(ref_date, route)
    record_key = dingtalk_record_key(ref_date)
    try:
        pipe = g_redis_client.pipeline(transaction=False)
        pipe.sadd(dedup_key, code)
        pipe.expire(dedup_key, REDIS_TTL_SEC)
        pipe.hset(record_key, mapping={code: encode_payload(record)})
        pipe.expire(record_key, REDIS_TTL_SEC)
        pipe.execute()
        return True
    except Exception as error:
        log(
            "[DINGTALK] success persist failed route=%s code=%s error=%s"
            % (route, code, error),
            "WARN",
            ref_date,
        )
        return False


def persist_dingtalk_success_with_retry(task, dingtalk_sent_at):
    for attempt in range(DINGTALK_PERSIST_RETRY_COUNT):
        if persist_dingtalk_success_once(task, dingtalk_sent_at):
            return True
        if attempt + 1 < DINGTALK_PERSIST_RETRY_COUNT:
            time.sleep(1.0 + attempt)
    return False


def dingtalk_persist_token(ref_date, route, code):
    return "%s:%s:%s" % (
        normalize_trade_date(ref_date),
        route or DINGTALK_ROUTE_YIN20,
        simple_code(code),
    )


def enqueue_dingtalk_persist_retry(task, dingtalk_sent_at):
    route = task.get("route", DINGTALK_ROUTE_YIN20)
    ref_date = normalize_trade_date(task.get("ref_date"))
    code = simple_code(task.get("code") or (task.get("result") or {}).get("code"))
    if not ref_date or not code:
        return False
    token = dingtalk_persist_token(ref_date, route, code)
    with g_dingtalk_lock:
        if token in g_dingtalk_persist_pending:
            return False
        g_dingtalk_persist_pending.add(token)
    g_dingtalk_persist_queue.put({
        "kind": "persist_only",
        "route": route,
        "ref_date": ref_date,
        "code": code,
        "sent_at": dingtalk_sent_at,
        "attempt": 0,
        "task": dict(task),
    })
    log(
        "[DINGTALK] queued persist-only retry route=%s code=%s"
        % (route, code),
        "WARN",
        ref_date,
    )
    return True


def process_dingtalk_persist_task(persist_task):
    task = persist_task.get("task") or {}
    route = persist_task.get("route", task.get("route", DINGTALK_ROUTE_YIN20))
    ref_date = normalize_trade_date(
        persist_task.get("ref_date") or task.get("ref_date")
    )
    code = simple_code(
        persist_task.get("code")
        or task.get("code")
        or (task.get("result") or {}).get("code")
    )
    dingtalk_sent_at = persist_task.get("sent_at", "")
    token = dingtalk_persist_token(ref_date, route, code)
    attempt = max(0, int(persist_task.get("attempt", 0) or 0))
    while True:
        if persist_dingtalk_success_once(task, dingtalk_sent_at):
            with g_dingtalk_lock:
                g_dingtalk_persist_pending.discard(token)
            log(
                "[DINGTALK] persist-only recovered route=%s code=%s attempts=%d"
                % (route, code, attempt + 1),
                "INFO",
                ref_date,
            )
            return True
        attempt += 1
        delay = min(
            DINGTALK_PERSIST_RETRY_MAX_DELAY_SEC,
            float(2 ** min(attempt, 5)),
        )
        log(
            "[DINGTALK] persist-only retry route=%s code=%s attempt=%d delay=%.0fs"
            % (route, code, attempt, delay),
            "WARN",
            ref_date,
        )
        time.sleep(delay)


def dingtalk_persist_worker():
    while True:
        persist_task = g_dingtalk_persist_queue.get()
        try:
            process_dingtalk_persist_task(persist_task)
        except Exception as error:
            log("[DINGTALK] persist worker error: %s" % error, "ERROR")
        finally:
            g_dingtalk_persist_queue.task_done()


def ensure_dingtalk_state(ref_date):
    global g_dingtalk_loaded_date, g_dingtalk_sent, g_dingtalk_pending
    if g_dingtalk_loaded_date == ref_date:
        return
    with g_dingtalk_lock:
        if g_dingtalk_loaded_date == ref_date:
            return
        sent = set()
        if g_redis_client is not None:
            for route in (DINGTALK_ROUTE_YIN20, DINGTALK_ROUTE_CONDITION3):
                try:
                    route_codes = g_redis_client.smembers(
                        dingtalk_dedup_key(ref_date, route)
                    )
                    sent.update(
                        dingtalk_task_token(route, value) for value in route_codes
                    )
                except Exception as error:
                    log(
                        "[DINGTALK] dedup load failed route=%s error=%s"
                        % (route, error),
                        "WARN",
                        ref_date,
                    )
        g_dingtalk_sent = sent
        g_dingtalk_pending = set()
        g_dingtalk_loaded_date = ref_date


def dingtalk_feature_names(result):
    labels = []
    for field, label in DINGTALK_FIVE_PULL_CONDITIONS:
        if int(result.get(field, 0) or 0) == 1:
            labels.append(label)
    try:
        yin_count_20 = int(result.get("yin_count_20"))
    except Exception:
        yin_count_20 = None
    if yin_count_20 is not None and yin_count_20 <= DINGTALK_MAX_YIN_COUNT_20:
        labels.append("20\u5929\u9634\u7ebf\u7b49\u4e8e%d" % yin_count_20)
    return labels


def dingtalk_display_width(value):
    width = 0
    for char in str(value or ""):
        width += 1 if ord(char) < 128 else 2
    return width


def pad_dingtalk_condition(value, target_width=21):
    text = str(value or "")
    missing = max(0, int(target_width) - dingtalk_display_width(text))
    return text + ("\u3000" * (missing // 2)) + (" " if missing % 2 else "")


def format_dingtalk_entry_conditions(result):
    result = result if isinstance(result, dict) else {}
    active = [
        label
        for field, label in DINGTALK_FIVE_PULL_CONDITIONS
        if int(result.get(field, 0) or 0) == 1
    ]
    lines = []
    for index in range(0, len(active), 2):
        if index + 1 < len(active):
            lines.append(pad_dingtalk_condition(active[index]) + active[index + 1])
        else:
            lines.append(active[index])
    try:
        yin_count_20 = int(result.get("yin_count_20"))
    except Exception:
        yin_count_20 = None
    if yin_count_20 is not None and yin_count_20 <= DINGTALK_MAX_YIN_COUNT_20:
        lines.append("**20\u5929\u9634\u7ebf\u7b49\u4e8e%d**" % yin_count_20)
    return "\n".join(lines) or "-"


def format_stock_alert(result, selected_at="", dingtalk_sent_at=""):
    features = dingtalk_feature_names(result)
    feature_text = "\n".join("#### %s" % value for value in features) or "#### -"
    try:
        yin_count_10d = int(result.get("yin_count_10d", 0) or 0)
    except Exception:
        yin_count_10d = 0
    selected_at = (
        selected_at
        or result.get("selected_at")
        or result.get("snapshot_at")
        or result.get("updated_at")
        or now_text()
    )
    dingtalk_sent_at = dingtalk_sent_at or now_text()
    text = (
        "**%s  %s \u6da8\u5e45: %.2f%%**\n\n"
        "\u9009\u4e2d\u65f6\u95f4: %s\n\n"
        "\u9489\u9489\u53d1\u9001\u65f6\u95f4: %s\n\n"
        "10\u5929\u5185\u9634\u7ebf\u6b21\u6570\uff1a%d\n\n"
        "**\u7279\u522b\u5f62\u6001:**\n%s\n\n"
        % (
            result.get("name", ""),
            result.get("code", ""),
            safe_float(result.get("ref_gain")),
            selected_at,
            dingtalk_sent_at,
            yin_count_10d,
            feature_text,
        )
    )
    text += (
        "\u63d0\u9192\uff1a  \n"
        "**1.\u4e00\u6d6a5\u65e5\u5706\u5f27\u6216\u8005\u5382\u5b50\u6700\u4f73\uff08\u6da8\u5e45\u4e0d\u8981\u592a\u5927\uff09**  \n"
        "**2.\u6700\u8fd1K\u7ebf\u4e0a\u4e0b\u5f15\u7ebf\u4e0d\u591a\uff08\u4e2d\u4f53\u4ea7\u4e1a\u4e8f\uff09**\n\n\n"
    )
    return "%s - \u76d8\u4e2d\u76d1\u63a7" % DINGTALK_SECURITY_KEYWORD, text


def should_send_stock_dingtalk(result, max_yin_count_20=4):
    result = result if isinstance(result, dict) else {}
    return (
        not is_risk_stock_name(result.get("name", ""))
        and safe_float(result.get("ref_gain")) >= MIN_REALTIME_GAIN_PCT
        and int(result.get("qiangchi", 0) or 0) == 1
    )


def dingtalk_routes_for_stock(result, max_yin_count_20=4):
    if should_send_stock_dingtalk(result, max_yin_count_20):
        return [DINGTALK_ROUTE_CONDITION3]
    return []


def enqueue_stock_alert(result, ref_date, routes=None):
    if not DINGTALK_ENABLED or not should_send_stock_dingtalk(result):
        return False
    if routes is None:
        routes = dingtalk_routes_for_stock(result, DINGTALK_MAX_YIN_COUNT_20)
    else:
        routes = list(routes)
    if not routes:
        return False
    code = simple_code(result.get("code"))
    if not code:
        return False
    ensure_dingtalk_state(ref_date)
    selected_at = (
        result.get("selected_at")
        or result.get("snapshot_at")
        or result.get("updated_at")
        or now_text()
    )
    queued = False
    for route in routes:
        token = dingtalk_task_token(route, code)
        with g_dingtalk_lock:
            if token in g_dingtalk_sent or token in g_dingtalk_pending:
                continue
            g_dingtalk_pending.add(token)
        try:
            g_dingtalk_queue.put_nowait({
                "kind": "stock",
                "route": route,
                "ref_date": ref_date,
                "code": code,
                "selected_at": selected_at,
                "result": dict(result),
            })
            queued = True
        except queue.Full:
            with g_dingtalk_lock:
                g_dingtalk_pending.discard(token)
            log(
                "[DINGTALK] queue full, dropped route=%s code=%s"
                % (route, code),
                "WARN",
                ref_date,
            )
    return queued


def send_dingtalk_markdown(title, text, webhook=None):
    try:
        import requests
        response = requests.post(
            webhook or DINGTALK_WEBHOOK,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        success = response.status_code == 200 and payload.get("errcode") == 0
        if not success:
            log(
                "[DINGTALK] response http=%s errcode=%s errmsg=%s body=%s"
                % (
                    response.status_code,
                    payload.get("errcode", ""),
                    payload.get("errmsg", ""),
                    str(response.text or "")[:300],
                ),
                "WARN",
            )
        return success
    except Exception as error:
        log("[DINGTALK] send error: %s" % error, "WARN")
        return False


def process_dingtalk_task(task):
    kind = task.get("kind")
    route = task.get("route", DINGTALK_ROUTE_YIN20)
    success = False
    dingtalk_sent_at = ""
    for attempt in range(DINGTALK_RETRY_COUNT):
        if kind == "stock":
            dingtalk_sent_at = now_text()
            title, text = format_stock_alert(
                task.get("result") or {},
                selected_at=task.get("selected_at", ""),
                dingtalk_sent_at=dingtalk_sent_at,
            )
        else:
            log("[DINGTALK] unsupported task kind=%s" % kind, "WARN")
            return False
        webhook = dingtalk_webhook_for_route(route)
        if send_dingtalk_markdown(title, text, webhook):
            success = True
            break
        if attempt + 1 < DINGTALK_RETRY_COUNT:
            time.sleep(1.0 + attempt)

    if kind == "stock":
        code = task.get("code", "")
        ref_date = task.get("ref_date", "")
        token = dingtalk_task_token(route, code)
        with g_dingtalk_lock:
            g_dingtalk_pending.discard(token)
            if success:
                g_dingtalk_sent.add(token)
        if success:
            persisted = persist_dingtalk_success_with_retry(
                task, dingtalk_sent_at
            )
            if not persisted:
                enqueue_dingtalk_persist_retry(task, dingtalk_sent_at)

    log(
        "[DINGTALK] kind=%s route=%s code=%s result=%s selected_at=%s sent_at=%s "
        "queue=%d pending=%d persist_queue=%d"
        % (
            kind,
            route,
            task.get("code", "-"),
            "success" if success else "failed_after_%d_attempts" % DINGTALK_RETRY_COUNT,
            task.get("selected_at", "-"),
            dingtalk_sent_at or "-",
            g_dingtalk_queue.qsize(),
            len(g_dingtalk_pending),
            g_dingtalk_persist_queue.qsize(),
        ),
        "INFO" if success else "WARN",
        task.get("ref_date", ""),
    )
    return success


def dingtalk_worker():
    while True:
        task = g_dingtalk_queue.get()
        try:
            process_dingtalk_task(task)
        except Exception as error:
            log("[DINGTALK] worker error: %s" % error, "ERROR")
        finally:
            g_dingtalk_queue.task_done()


# ==================== scan ====================

def ladder_at_limit(price, limit_price):
    price, limit_price = safe_float(price), safe_float(limit_price)
    return min(price, limit_price) > 0 and abs(price - limit_price) < 0.0001


def ladder_history_rows(frame, ref_date):
    if frame is None:
        return []
    rows = []
    previous = 0.0
    for index in range(len(frame)):
        raw_date = frame.index[index]
        date = raw_date.strftime("%Y%m%d") if hasattr(raw_date, "strftime") else normalize_trade_date(raw_date)
        close = safe_float(frame["close"].iloc[index])
        if not date or date >= ref_date or close <= 0:
            continue
        pre_close = safe_float(frame["preClose"].iloc[index]) if "preClose" in frame else 0.0
        row = {"date": date, "close": close, "pre_close": pre_close or previous}
        if "open" in frame:
            row["open"] = safe_float(frame["open"].iloc[index])
        rows.append(row)
        previous = close
    return rows


def ladder_board_streak(stock, name, rows, current_close, current_limit, open_date=""):
    if not ladder_at_limit(current_close, current_limit):
        return 0, False
    ratio = get_limit_up_ratio(stock) / 100.0
    if simple_code(stock).startswith("689"):
        ratio = 0.20
    no_limit_dates = set()
    listing_date = normalize_trade_date(open_date)
    for index, row in enumerate(rows):
        if listing_date and row["date"] == listing_date:
            count = 1 if str(stock).endswith(".BJ") else 5
            no_limit_dates = {item["date"] for item in rows[index:index + count]}
            break
    streak = 1
    complete = False
    for row in reversed(rows):
        if row["date"] in no_limit_dates:
            complete = True
            break
        previous = safe_float(row.get("pre_close"))
        if previous <= 0:
            break
        day_ratio = ratio
        # SH/SZ main-board ST limits changed from 5% to 10% on 2026-07-06.
        if ratio == 0.10 and "ST" in str(name).upper() and row["date"] < "20260706":
            day_ratio = 0.05
        upper = float((Decimal(str(previous)) * (Decimal("1") + Decimal(str(day_ratio)))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ))
        if not ladder_at_limit(row.get("close"), upper):
            complete = True
            break
        streak += 1
    return streak, complete


def init_ladder_market(ContextInfo, base_pool):
    global g_ladder_pool, g_ladder_details, g_ladder_details_date
    global g_ladder_history_date, g_ladder_history, g_ladder_history_retry_at
    pool = set(base_pool)
    # QMT versions use different names for the Beijing market sector.
    for sector in ("\u5317\u4ea4\u6240", "\u5317\u8bc1A\u80a1", "\u5317\u4eacA\u80a1"):
        try:
            pool.update(ContextInfo.get_stock_list_in_sector(sector) or [])
        except Exception:
            pass
    if not any(str(stock).endswith(".BJ") for stock in pool):
        try:
            market_ticks = ContextInfo.get_full_tick(["BJ"])
            if isinstance(market_ticks, dict):
                pool.update(stock for stock in market_ticks if str(stock).endswith(".BJ"))
        except Exception as error:
            log("[LADDER] Beijing market discovery unavailable: %s" % error, "WARN")
    pool = sorted(stock for stock in pool if (
        str(stock).endswith((".SH", ".SZ", ".BJ"))
        and simple_code(stock).startswith(("0", "3", "6", "8", "4", "92"))
    ))
    details = {}
    get_detail = getattr(ContextInfo, "get_instrument_detail", None)
    if not callable(get_detail):
        get_detail = getattr(ContextInfo, "get_instrumentdetail", None)
    for stock in pool:
        try:
            detail = get_detail(stock) if callable(get_detail) else None
            if isinstance(detail, dict) and detail:
                details[stock] = detail
        except Exception:
            pass
    g_ladder_pool = pool
    g_ladder_details = details
    g_ladder_details_date = datetime.now().strftime("%Y%m%d")
    g_ladder_history_date, g_ladder_history, g_ladder_history_retry_at = "", {}, {}
    log("[LADDER] market=%d details=%d BJ=%d; existing strategy universe unchanged" % (
        len(pool), len(details), sum(stock.endswith(".BJ") for stock in pool)
    ))


def build_limit_up_ladder(ContextInfo, all_ticks, ref_date, snapshot_at):
    global g_ladder_history_date, g_ladder_history, g_ladder_history_retry_at
    metadata = {
        "status": "success", "scope": "all_a_shares", "reference_date": ref_date,
        "updated_at": snapshot_at, "market_count": len(g_ladder_pool),
        "market_counts": {market: sum(stock.endswith("." + market) for stock in g_ladder_pool)
                          for market in ("SH", "SZ", "BJ")},
        "missing_quote_count": 0, "missing_detail_count": 0,
        "history_missing_count": 0, "streak_pending_count": 0,
        "window_metrics_missing_count": 0,
        "history_rule": "raw_preclose_board_ratio",
    }
    if ref_date != g_ladder_details_date:
        metadata.update(status="unavailable", error="No current-day instrument limits for this date")
        return [], metadata
    if g_ladder_history_date != ref_date:
        g_ladder_history_date, g_ladder_history, g_ladder_history_retry_at = ref_date, {}, {}
    candidates = []
    for stock in g_ladder_pool:
        tick = all_ticks.get(stock) or {}
        if tick_trade_date(tick) != ref_date or safe_float(tick.get("lastPrice")) <= 0:
            metadata["missing_quote_count"] += 1
            continue
        detail = g_ladder_details.get(stock) or {}
        if not detail:
            metadata["missing_detail_count"] += 1
            continue
        if ladder_at_limit(tick.get("lastPrice"), detail.get("UpStopPrice")):
            candidates.append((stock, tick, detail))
    now_ts = time.time()
    needed = [stock for stock, _, _ in candidates if stock not in g_ladder_history
              and now_ts >= g_ladder_history_retry_at.get(stock, 0)]
    for offset in range(0, len(needed), HISTORY_BATCH_SIZE):
        batch = needed[offset:offset + HISTORY_BATCH_SIZE]
        try:
            data = ContextInfo.get_market_data_ex(
                ["open", "close", "preClose"], batch, period="1d", end_time=ref_date + "235959",
                count=HISTORY_REQUEST_DAYS, dividend_type="none", fill_data=False,
            )
            for stock in batch:
                frame = extract_stock_data(data, stock)
                rows = ladder_history_rows(frame, ref_date)
                if rows:
                    g_ladder_history[stock] = rows
                else:
                    g_ladder_history_retry_at[stock] = now_ts + 60
        except Exception as error:
            for stock in batch:
                g_ladder_history_retry_at[stock] = now_ts + 60
            log("[LADDER] raw history failed: %s" % error, "WARN", ref_date)
    results = []
    for stock, tick, detail in candidates:
        name = str(detail.get("InstrumentName") or g_stock_names.get(stock) or simple_code(stock))
        previous = safe_float(detail.get("PreClose")) or safe_float(tick.get("lastClose"))
        close = safe_float(tick.get("lastPrice"))
        high_gain = calc_gain(max(safe_float(tick.get("high")), close), previous)
        rows = g_ladder_history.get(stock, [])
        streak, complete = ladder_board_streak(
            stock, name, rows, close, detail.get("UpStopPrice"), detail.get("OpenDate")
        )
        if not rows:
            metadata["history_missing_count"] += 1
        if not complete:
            metadata["streak_pending_count"] += 1
        recent = rows[-19:] + [{"open": safe_float(tick.get("open")), "close": close}]
        opens = [safe_float(row.get("open")) for row in recent]
        closes = [safe_float(row.get("close")) for row in recent]
        yin_count_20 = (sum(day_close < day_open for day_open, day_close in zip(opens, closes))
                        if len(recent) == 20 and min(opens + closes) > 0 else None)
        independent_10d = None
        if len(recent) >= 10 and min(opens[-10:] + closes[-10:]) > 0:
            independent_10d = calculate_reference_candle_flags(
                stock, opens, closes, previous
            )["yi_yin_yi_yang_10d"]
        if yin_count_20 is None or independent_10d is None:
            metadata["window_metrics_missing_count"] += 1
        results.append({
            "code": simple_code(stock), "stock_code": stock, "name": name,
            "ref_date": ref_date, "ref_gain": round(calc_gain(close, previous), 6),
            "today_change": round(calc_gain(close, previous), 6),
            "ref_high_gain": round(high_gain, 6), "max_gain": round(high_gain, 6),
            "ref_high_gain_ok": int(high_gain >= 6.5),
            "ref_high_gain_gte_6_5": int(high_gain >= 6.5),
            "is_bare_foot": int(is_nearly_bare_foot(
                tick.get("open"), close, tick.get("high"), tick.get("low")
            )),
            "is_limit_up": 1, "ref_is_limit_up": 1,
            "limit_up_price": safe_float(detail.get("UpStopPrice")),
            "consecutive_limit_up_days": streak if complete else None,
            "streak_min_days": streak,
            "streak_complete": complete,
            "is_first_limit_up": int(complete and streak == 1),
            "is_second_limit_up": int(complete and streak == 2),
            "yin_count_20": yin_count_20,
            "yi_yin_yi_yang_10d": independent_10d,
            "updated_at": snapshot_at,
        })
    metadata["selected_count"] = len(results)
    metadata["missing_markets"] = [market for market, count in metadata["market_counts"].items() if not count]
    if (metadata["missing_markets"] or metadata["missing_quote_count"]
            or metadata["missing_detail_count"] or metadata["streak_pending_count"]):
        metadata["status"] = "partial"
    return results, metadata


def publish_limit_up_ladder(ContextInfo, all_ticks, ref_date, snapshot_at):
    started = time.time()
    if not g_ladder_pool or not all_ticks:
        return 0.0
    try:
        results, metadata = build_limit_up_ladder(ContextInfo, all_ticks, ref_date, snapshot_at)
        if metadata["status"] == "unavailable" or not g_ladder_pool:
            return 0.0
        if metadata.get("market_count", 0) > 0 and (
            metadata.get("missing_quote_count", 0) >= metadata["market_count"]
        ):
            log("[LADDER] no valid current-day quotes; retain last snapshot", "WARN", ref_date)
            return time.time() - started
        mapping = {item["code"]: item for item in results}
        metadata["seconds"] = round(time.time() - started, 4)
        mapping["_scan_info"] = metadata
        publish_full_hash(redis_key(LIMIT_UP_LADDER_PREFIX, ref_date), mapping)
        return time.time() - started
    except Exception as error:
        log("[LADDER] publish failed; existing strategy snapshots unaffected: %s" % error,
            "WARN", ref_date)
        return time.time() - started


def get_realtime_ticks(ContextInfo):
    try:
        pool = sorted(set(g_stock_pool).union(g_ladder_pool)) if g_ladder_pool else g_stock_pool
        ticks = ContextInfo.get_full_tick(pool)
        return ticks if isinstance(ticks, dict) else {}
    except Exception as error:
        log("[TICK] get_full_tick failed: %s" % error, "ERROR", g_reference_date)
        return {}


def latest_tick_trade_date(all_ticks):
    latest = ""
    for tick in (all_ticks or {}).values():
        value = tick_trade_date(tick)
        if value and value > latest:
            latest = value
    return latest


def build_pz1_result(stock, name, series, ref_date, snapshot_at, metrics=None, shared=None):
    opens = list(series.get("opens") or [])
    highs = list(series.get("highs") or [])
    lows = list(series.get("lows") or [])
    closes = list(series.get("closes") or [])
    size = min(len(opens), len(highs), len(lows), len(closes))
    if size < 20:
        return None

    if metrics is None:
        metrics = calculate_window_metrics(opens, highs, lows, closes)

    current_price = safe_float(closes[-1])
    previous_close = safe_float(series.get("pre_close"))
    if previous_close <= 0 and len(closes) >= 2:
        previous_close = safe_float(closes[-2])
    ref_gain = calc_gain(current_price, previous_close)
    max_gain = calc_gain(highs[-1], previous_close)
    reference_flags = shared_series_value(
        shared, "reference_candle_flags", lambda: calculate_reference_candle_flags(
            stock, opens, closes, previous_close
        )
    )
    yi_yin_yi_yang_flags = calculate_yi_yin_yi_yang_flags(
        stock, opens, closes, metrics["high_gain_10d_count"], previous_close
    )
    is_ref_price_20d_high = int(
        current_price + 1e-8 >= shared_series_value(
            shared, "prior_high_20d", lambda: max(safe_float(value) for value in highs[-20:-1])
        )
    )
    prev3_metrics = shared_series_value(
        shared, "prev3_metrics", lambda: calculate_prev3_shadow_metrics(
            opens, highs, lows, closes, reference_index=size - 1
        )
    )
    high_gain_prev3_short_shadow_met = int(
        max_gain + 1e-9 >= HIGH_GAIN_THRESHOLD_PCT
        and prev3_metrics["prev3_short_shadow_met"] == 1
    )

    five_pull_group_flags = calculate_five_pull_group_flags(metrics)
    yin_count_20_lte_4 = int(metrics["yin_count_20"] <= 4)
    yin_count_20_lte_7_and_ref_yang = int(
        metrics["yin_count_20"] <= PZ1_MAX_YIN_COUNT_20
        and current_price > opens[-1]
    )
    yin7_yang_no_touch_or_eaten_10d = int(
        yin_count_20_lte_7_and_ref_yang
        and shared_series_value(shared, "qy1_metrics", lambda: calculate_qy1_metrics(opens, highs, lows, closes))[
            "all_yin_no_touch_or_eaten_10d"
        ] == 1
    )
    quanyang_10d_met = int(metrics.get("is_quanyang_10d", 0) or 0)
    five_pull_met = int(
        int(metrics.get("high_gain_10d_count", 0) or 0) >= MIN_HIGH_GAIN_COUNT_10D
    )
    yin_pull_next_or_eaten_met = int(
        five_pull_group_flags.get("five_pull_yin_pull_next", 0) or 0
    )
    limit_up_gap_flags = shared_series_value(
        shared, "limit_up_gap_flags", lambda: calculate_limit_up_gap_flags(stock, series)
    )
    entry_flags = (
        ("yin_count_20_lte_7_and_ref_yang", yin_count_20_lte_7_and_ref_yang),
        ("quanyang_10d", quanyang_10d_met),
        ("high_gain_10d_gte_5", five_pull_met),
        ("high_gain_prev3_short_shadow", high_gain_prev3_short_shadow_met),
        ("yi_yin_yi_yang_10d", reference_flags["yi_yin_yi_yang_10d"]),
        ("yi_yin_yi_yang_20d", reference_flags["yi_yin_yi_yang_20d"]),
        ("yi_yin_yi_yang", yi_yin_yi_yang_flags["yi_yin_yi_yang"]),
        ("limit_up_gap_yin_lte1_30d", limit_up_gap_flags["limit_up_gap_yin_lte1_30d"]),
    )
    if not any(value for _field, value in entry_flags):
        return None
    yin_below_ma5_stats = calculate_yin_close_below_ma5_stats_20d(
        opens, closes
    )
    code = simple_code(stock)
    limit_ratio = get_limit_up_ratio(stock)
    is_limit_up = int(is_limit_up_by_price(current_price, previous_close, stock))
    is_high_gain = int(max_gain + 1e-9 >= HIGH_GAIN_THRESHOLD_PCT)
    is_bare_foot = int(
        is_nearly_bare_foot(opens[-1], current_price, highs[-1], lows[-1])
    )
    nearly_bare_foot_count_10d = 0
    bare_foot_count_10d = 0
    for index in range(size - 10, size):
        day_open = safe_float(opens[index])
        day_low = safe_float(lows[index])
        if is_nearly_bare_foot(day_open, closes[index], highs[index], day_low):
            nearly_bare_foot_count_10d += 1
        if day_low > 0 and day_low == day_open:
            bare_foot_count_10d += 1
    result = {
        "code": code,
        "name": name or code,
        "stock_code_with_market": stock,
        "ref_date": ref_date,
        "current_price": round(current_price, 4),
        "prev_close": round(previous_close, 4),
        "ref_gain": round(ref_gain, 4),
        "today_change": round(ref_gain, 4),
        "max_gain": round(max_gain, 4),
        "is_ref_price_20d_high": is_ref_price_20d_high,
        "ref_high_gain_ok": bool(is_high_gain),
        "is_high_gain": is_high_gain,
        "limit_ratio": limit_ratio,
        "is_limit_up": is_limit_up,
        "ref_is_limit_up": bool(is_limit_up),
        "is_ref_yang": int(current_price > opens[-1]),
        "is_bare_foot": is_bare_foot,
        "nearly_bare_foot_count_10d": nearly_bare_foot_count_10d,
        "bare_foot_count_10d": bare_foot_count_10d,
        "prev3_short_shadow_met": int(
            prev3_metrics["prev3_short_shadow_met"]
        ),
        "prev3_yang_count": int(prev3_metrics["prev3_yang_count"]),
        "yin_count_20": int(metrics["yin_count_20"]),
        "close_below_ma5_count_20d": yin_below_ma5_stats["close_below_ma5_count_20d"],
        "yin_close_below_ma5_max_abs_pct_20d": yin_below_ma5_stats[
            "yin_close_below_ma5_max_abs_pct_20d"
        ],
        "yin_close_below_ma5_drop_pct_sum_20d": yin_below_ma5_stats[
            "yin_close_below_ma5_drop_pct_sum_20d"
        ],
        "yin_close_below_ma5_body_pct_sum_20d": yin_below_ma5_stats[
            "yin_close_below_ma5_body_pct_sum_20d"
        ],
        "yin_count_10d": int(metrics["yin_count_10d"]),
        "high_gain_10d_count": int(metrics["high_gain_10d_count"]),
        "low_above_ma5_count_10d": int(metrics["low_above_ma5_count_10d"]),
        "all_close_above_ma10_10d": int(metrics["all_close_above_ma10_10d"]),
        "ref_gain_gte_5": int(ref_gain >= MIN_REALTIME_GAIN_PCT),
        "yin_count_20_lte_6": int(metrics["yin_count_20"] <= 6),
        "high_gain_10d_gte_5": five_pull_met,
        "low_above_ma5_10d_gte_5": int(
            metrics["low_above_ma5_count_10d"] >= MIN_LOW_ABOVE_MA5_COUNT_10D
        ),
        "condition_2_met": int(metrics["yin_count_20"] <= MAX_YIN_COUNT_20),
        "condition_3_met": int(
            metrics["high_gain_10d_count"] >= MIN_HIGH_GAIN_COUNT_10D
            and metrics["all_close_above_ma10_10d"] == 1
        ),
        "condition_4_met": int(
            metrics["low_above_ma5_count_10d"] >= MIN_LOW_ABOVE_MA5_COUNT_10D
        ),
        "all_yin_no_touch": int(metrics["all_yin_no_touch"]),
        "all_yin_up": int(metrics["all_yin_up"]),
        "all_yin_cross_star": int(metrics["all_yin_cross_star"]),
        "yin_pull_next": int(metrics["yin_pull_next"]),
        "yin_pull_prev_or_next": int(metrics["yin_pull_prev_or_next"]),
        "all_yin_eaten_next": int(metrics["all_yin_eaten_next"]),
        "continuous_4_no_touch": int(metrics["continuous_4_no_touch"]),
        "is_quanyang_10d": int(metrics["is_quanyang_10d"]),
        "five_pull_group_matched": int(any(five_pull_group_flags.values())),
        "five_pull_groups": [
            field for field, value in five_pull_group_flags.items() if int(value) == 1
        ],
        "pz1_direct_met": 1,
        "pz1_yin_count_20_lte_4": yin_count_20_lte_4,
        "pz1_yin_count_20_lte_7_and_ref_yang": yin_count_20_lte_7_and_ref_yang,
        "pz1_yin7_yang_no_touch_or_eaten_10d": yin7_yang_no_touch_or_eaten_10d,
        "pz1_quanyang_10d_met": quanyang_10d_met,
        "pz1_five_pull_met": five_pull_met,
        "yi_yin_yi_yang": yi_yin_yi_yang_flags["yi_yin_yi_yang"],
        "qiangchi": yi_yin_yi_yang_flags["qiangchi"],
        "pz1_yin_pull_next_or_eaten_met": yin_pull_next_or_eaten_met,
        "pz1_high_gain_prev3_short_shadow_met": (
            high_gain_prev3_short_shadow_met
        ),
        "match_reasons": [field for field, value in entry_flags if value],
        "selected_at": snapshot_at,
        "snapshot_at": snapshot_at,
        "updated_at": now_text(),
    }
    result.update(reference_flags)
    result.update(limit_up_gap_flags)
    result.update(five_pull_group_flags)
    result["five_pull_yin_pull_next_or_eaten"] = int(
        five_pull_group_flags.get("five_pull_yin_pull_next", 0) or 0
    )
    return result


def calculate_yin_close_below_ma5_stats_20d(opens, closes):
    fields = (
        "close_below_ma5_count_20d",
        "yin_close_below_ma5_max_abs_pct_20d",
        "yin_close_below_ma5_drop_pct_sum_20d",
        "yin_close_below_ma5_body_pct_sum_20d",
    )
    if min(len(opens), len(closes)) < 24:
        return dict.fromkeys(fields)
    recent_closes = [safe_float(value) for value in closes[-24:]]
    recent_opens = [safe_float(value) for value in opens[-20:]]
    if any(value <= 0 for value in recent_closes + recent_opens):
        return dict.fromkeys(fields)

    count = 0
    max_abs_pct = 0.0
    drop_pct_sum = 0.0
    body_pct_sum = 0.0
    for index in range(4, 24):
        close_price = recent_closes[index]
        open_price = recent_opens[index - 4]
        if close_price >= open_price:
            continue
        ma5 = sum(recent_closes[index - 4:index + 1]) / 5.0
        if close_price >= ma5:
            continue
        count += 1
        max_abs_pct = max(max_abs_pct, (ma5 - close_price) / ma5 * 100.0)
        previous_close = recent_closes[index - 1]
        drop_pct_sum += max(0.0, (previous_close - close_price) / previous_close * 100.0)
        body_pct_sum += (open_price - close_price) / open_price * 100.0

    return {
        "close_below_ma5_count_20d": count,
        "yin_close_below_ma5_max_abs_pct_20d": round(max_abs_pct, 6),
        "yin_close_below_ma5_drop_pct_sum_20d": round(drop_pct_sum, 6),
        "yin_close_below_ma5_body_pct_sum_20d": round(body_pct_sum, 6),
    }


def calculate_close_below_ma5_count_20d(opens, closes):
    return calculate_yin_close_below_ma5_stats_20d(opens, closes)[
        "close_below_ma5_count_20d"
    ]


def calculate_low_above_ma5_count_20d(closes, lows):
    size = min(len(closes), len(lows))
    count = 0
    for index in range(max(4, size - 20), size):
        ma5 = sum(safe_float(value) for value in closes[index - 4:index + 1]) / 5.0
        if ma5 > 0 and safe_float(lows[index]) > ma5:
            count += 1
    return count


def build_qy_common_result(stock, name, series, ref_date, snapshot_at,
                           metrics, qy1_metrics, shared=None):
    opens = list(series.get("opens") or [])
    highs = list(series.get("highs") or [])
    lows = list(series.get("lows") or [])
    closes = list(series.get("closes") or [])
    current_price = safe_float(closes[-1])
    previous_close = safe_float(series.get("pre_close"))
    if previous_close <= 0 and len(closes) >= 2:
        previous_close = safe_float(closes[-2])
    ref_gain = calc_gain(current_price, previous_close)
    max_gain = calc_gain(highs[-1], previous_close)
    code = simple_code(stock)
    limit_ratio = get_limit_up_ratio(stock)
    is_limit_up = int(is_limit_up_by_price(current_price, previous_close, stock))
    is_high_gain = int(max_gain >= HIGH_GAIN_THRESHOLD_PCT)
    is_bare_foot = int(
        is_nearly_bare_foot(opens[-1], current_price, highs[-1], lows[-1])
    )
    result = {
        "code": code,
        "name": name or code,
        "stock_code_with_market": stock,
        "ref_date": ref_date,
        "current_price": round(current_price, 4),
        "prev_close": round(previous_close, 4),
        "ref_gain": round(ref_gain, 4),
        "today_change": round(ref_gain, 4),
        "max_gain": round(max_gain, 4),
        "ref_high_gain_ok": bool(is_high_gain),
        "is_high_gain": is_high_gain,
        "limit_ratio": limit_ratio,
        "is_limit_up": is_limit_up,
        "ref_is_limit_up": bool(is_limit_up),
        "is_ref_yang": int(current_price > opens[-1]),
        "is_bare_foot": is_bare_foot,
        "yin_count_20": int(metrics["yin_count_20"]),
        "yin_count_10d": int(metrics["yin_count_10d"]),
        "is_quanyang_10d": int(metrics["is_quanyang_10d"]),
        "all_yin_up": int(metrics["all_yin_up"]),
        "all_yin_no_touch": int(metrics["all_yin_no_touch"]),
        "all_yin_cross_star": int(metrics["all_yin_cross_star"]),
        "high_gain_10d_count": int(metrics["high_gain_10d_count"]),
        "low_above_ma5_count_10d": int(metrics["low_above_ma5_count_10d"]),
        "low_above_ma5_count_20d": shared_series_value(
            shared, "low_above_ma5_count_20d", lambda: calculate_low_above_ma5_count_20d(closes, lows)
        ),
        "continuous_4_no_touch": int(metrics["continuous_4_no_touch"]),
        "lower_shadow_pct_sum_4d": metrics["lower_shadow_pct_sum_4d"],
        "all_close_above_ma10_10d": int(
            metrics["all_close_above_ma10_10d"]
        ),
        "ref_gain_gte_5": int(ref_gain >= MIN_REALTIME_GAIN_PCT),
        "qy1_all_yin_no_touch_or_eaten_10d": int(
            qy1_metrics["all_yin_no_touch_or_eaten_10d"]
        ),
        "all_yin_no_touch_or_eaten_10d": int(
            qy1_metrics["all_yin_no_touch_or_eaten_10d"]
        ),
        "strong_k_count_10d": int(qy1_metrics["strong_k_count_10d"]),
        "selected_at": snapshot_at,
        "snapshot_at": snapshot_at,
        "updated_at": now_text(),
    }
    result.update(shared_series_value(
        shared, "reference_candle_flags", lambda: calculate_reference_candle_flags(
            stock, opens, closes, previous_close
        )
    ))
    result.update(shared_series_value(
        shared, "limit_up_gap_flags", lambda: calculate_limit_up_gap_flags(stock, series)
    ))
    return result


def build_qy1_result(stock, name, series, ref_date, snapshot_at, metrics=None, shared=None):
    opens = list(series.get("opens") or [])
    highs = list(series.get("highs") or [])
    lows = list(series.get("lows") or [])
    closes = list(series.get("closes") or [])
    if min(len(opens), len(highs), len(lows), len(closes)) < 20:
        return None
    current_price = safe_float(closes[-1])
    previous_close = safe_float(series.get("pre_close"))
    if previous_close <= 0 and len(closes) >= 2:
        previous_close = safe_float(closes[-2])
    if calc_gain(current_price, previous_close) < MIN_REALTIME_GAIN_PCT:
        return None
    qy1_metrics = shared_series_value(shared, "qy1_metrics", lambda: calculate_qy1_metrics(opens, highs, lows, closes))
    if int(qy1_metrics["all_yin_no_touch_or_eaten_10d"]) != 1:
        return None
    if metrics is None:
        metrics = shared_series_value(shared, "window_metrics", lambda: calculate_window_metrics(opens, highs, lows, closes))
    result = build_qy_common_result(
        stock, name, series, ref_date, snapshot_at, metrics, qy1_metrics, shared=shared
    )
    result["close_below_ma5_count_10d"] = int(
        qy1_metrics.get("close_below_ma5_count_10d", 0)
    )
    result["close_below_ma5_abs_pct_sum_10d"] = round(
        safe_float(qy1_metrics.get("close_below_ma5_abs_pct_sum_10d")), 6
    )
    result["match_reasons"] = ["all_yin_no_touch_or_eaten_10d"]
    return result


def build_qy2_result(stock, name, series, ref_date, snapshot_at, metrics=None, shared=None):
    opens = list(series.get("opens") or [])
    highs = list(series.get("highs") or [])
    lows = list(series.get("lows") or [])
    closes = list(series.get("closes") or [])
    if min(len(opens), len(highs), len(lows), len(closes)) < 20:
        return None
    qy2_metrics = calculate_scan_qy2_metrics(
        shared.get("scan_window") if shared is not None else None,
        opens, lows, closes
    )
    if qy2_metrics["all_yin_low_gte_ma5_or_eaten_10d"] != 1:
        return None
    if metrics is None:
        metrics = shared_series_value(shared, "window_metrics", lambda: calculate_window_metrics(opens, highs, lows, closes))
    qy1_metrics = shared_series_value(shared, "qy1_metrics", lambda: calculate_qy1_metrics(opens, highs, lows, closes))
    result = build_qy_common_result(
        stock, name, series, ref_date, snapshot_at, metrics, qy1_metrics, shared=shared
    )
    result.update({
        "qy2_all_yin_low_gte_ma5_or_eaten_10d": 1,
        "qy2_all_yin_eaten_next_10d": qy2_metrics["all_yin_eaten_next_10d"],
        "all_yin_eaten_next_10d": qy2_metrics["all_yin_eaten_next_10d"],
        "close_below_ma5_count_10d": int(
            qy2_metrics.get("close_below_ma5_count_10d", 0)
        ),
        "close_below_ma5_10d_count": int(
            qy2_metrics.get("close_below_ma5_count_10d", 0)
        ),
        "close_below_ma5_abs_pct_sum_10d": round(
            safe_float(qy2_metrics.get("close_below_ma5_abs_pct_sum_10d")), 6
        ),
        "high_gain_10d_gte_5": int(
            metrics["high_gain_10d_count"] >= MIN_HIGH_GAIN_COUNT_10D
        ),
        "match_reasons": ["all_yin_low_gte_ma5_or_eaten_10d"],
    })
    return result


def evaluate_candidate(stock, name, series, tick, ref_date, snapshot_at, metrics=None,
                       published_codes=None, payload_cache=None, shared=None):
    if len(series.get("closes") or []) < 20:
        return None, None, None
    if metrics is None:
        metrics = calculate_window_metrics(
            series["opens"], series["highs"], series["lows"], series["closes"]
        )
    five_pull_group_flags = calculate_five_pull_group_flags(metrics)
    matched, reasons = passes_secondary_conditions(
        metrics["yin_count_20"],
        metrics["high_gain_10d_count"],
        metrics["all_close_above_ma10_10d"],
        metrics["low_above_ma5_count_10d"],
        five_pull_group_flags,
    )
    if not matched:
        return None, None, None
    current_price = safe_float(series["closes"][-1])
    previous_close = safe_float(series.get("pre_close"))
    if previous_close <= 0 and len(series["closes"]) >= 2:
        previous_close = safe_float(series["closes"][-2])
    ref_gain = calc_gain(current_price, previous_close)
    max_gain = calc_gain(series["highs"][-1], previous_close)
    code = simple_code(stock)
    limit_ratio = get_limit_up_ratio(stock)
    is_limit_up = int(is_limit_up_by_price(current_price, previous_close, stock))
    is_bare_foot = int(
        is_nearly_bare_foot(
            series["opens"][-1],
            current_price,
            series["highs"][-1],
            series["lows"][-1],
        )
    )
    result = {
        "code": code,
        "name": name or code,
        "stock_code_with_market": stock,
        "ref_date": ref_date,
        "current_price": round(current_price, 4),
        "prev_close": round(previous_close, 4),
        "ref_gain": round(ref_gain, 4),
        "today_change": round(ref_gain, 4),
        "max_gain": round(max_gain, 4),
        "limit_ratio": limit_ratio,
        "is_limit_up": is_limit_up,
        "ref_is_limit_up": bool(is_limit_up),
        "is_ref_yang": int(current_price > series["opens"][-1]),
        "is_bare_foot": is_bare_foot,
        "yin_count_20": metrics["yin_count_20"],
        "yin_count_10d": metrics["yin_count_10d"],
        "high_gain_10d_count": metrics["high_gain_10d_count"],
        "low_above_ma5_count_10d": metrics["low_above_ma5_count_10d"],
        "low_above_ma5_count_20d": shared_series_value(
            shared, "low_above_ma5_count_20d", lambda: calculate_low_above_ma5_count_20d(series["closes"], series["lows"])
        ),
        "all_close_above_ma10_10d": metrics["all_close_above_ma10_10d"],
        "ref_gain_gte_5": 1,
        "yin_count_20_lte_6": int(metrics["yin_count_20"] <= 6),
        "high_gain_10d_gte_5": int(metrics["high_gain_10d_count"] >= MIN_HIGH_GAIN_COUNT_10D),
        "low_above_ma5_10d_gte_5": int(metrics["low_above_ma5_count_10d"] >= MIN_LOW_ABOVE_MA5_COUNT_10D),
        "condition_2_met": int(metrics["yin_count_20"] <= MAX_YIN_COUNT_20),
        "condition_3_met": int(
            metrics["high_gain_10d_count"] >= MIN_HIGH_GAIN_COUNT_10D
            and metrics["all_close_above_ma10_10d"] == 1
        ),
        "condition_4_met": int(
            metrics["low_above_ma5_count_10d"] >= MIN_LOW_ABOVE_MA5_COUNT_10D
        ),
        "all_yin_no_touch": metrics["all_yin_no_touch"],
        "all_yin_up": metrics["all_yin_up"],
        "all_yin_cross_star": metrics["all_yin_cross_star"],
        "yin_pull_next": metrics["yin_pull_next"],
        "yin_pull_prev_or_next": metrics["yin_pull_prev_or_next"],
        "all_yin_eaten_next": metrics["all_yin_eaten_next"],
        "continuous_4_no_touch": metrics["continuous_4_no_touch"],
        "is_quanyang_10d": metrics["is_quanyang_10d"],
        "five_pull_group_matched": int(any(five_pull_group_flags.values())),
        "five_pull_groups": [
            field for field, value in five_pull_group_flags.items() if int(value) == 1
        ],
        "match_reasons": reasons,
        "selected_at": snapshot_at,
        "snapshot_at": snapshot_at,
        "updated_at": now_text(),
    }
    result.update(shared_series_value(
        shared, "reference_candle_flags", lambda: calculate_reference_candle_flags(
            stock, series["opens"], series["closes"], previous_close
        )
    ))
    result.update(shared_series_value(
        shared, "limit_up_gap_flags", lambda: calculate_limit_up_gap_flags(stock, series)
    ))
    result.update(five_pull_group_flags)
    result["five_pull_yin_pull_next_or_eaten"] = int(
        five_pull_group_flags.get("five_pull_yin_pull_next", 0) or 0
    )
    history_payload, realtime_payload = build_kline_payloads(
        stock, name, series, ref_date, snapshot_at,
        published_codes=published_codes, payload_cache=payload_cache
    )
    return result, history_payload, realtime_payload


def scan_stocks(ContextInfo=None, force=False):
    global g_scan_count, g_busy_skip_count, g_historical_done, g_reference_date
    global g_last_cycle_started_ts
    ContextInfo = ContextInfo or g_context
    if ContextInfo is None or g_redis_client is None:
        return None
    current_time = datetime.now()
    historical_mode = bool(normalize_trade_date(REFERENCE_DATE))
    if historical_mode and g_historical_done:
        return "historical_done"
    if CHECK_TRADING_TIME and not historical_mode and not force and not is_trading_session(current_time):
        return "outside_trading_time"
    if not g_scan_lock.acquire(blocking=False):
        g_busy_skip_count += 1
        return "busy_skip"

    started = time.time()
    started_at = now_text(current_time)
    actual_interval_seconds = (
        started - g_last_cycle_started_ts if g_last_cycle_started_ts else 0.0
    )
    g_last_cycle_started_ts = started
    stage = "resolve_date"
    ref_date = ""
    fallback_performance_date = (
        normalize_trade_date(REFERENCE_DATE) or current_time.strftime("%Y%m%d")
    )
    try:
        ref_date = resolve_reference_date(current_time)
        g_scan_count += 1
        snapshot_at = now_text(current_time)
        log(
            "[CYCLE-START] date=%s cycle=%d trigger=%s interval=%.3fs "
            "market=%d history=%d ding_queue=%d ding_pending=%d"
            % (
                ref_date,
                g_scan_count,
                "force" if force else "timer",
                actual_interval_seconds,
                len(g_stock_pool),
                len(g_history_data),
                g_dingtalk_queue.qsize(),
                len(g_dingtalk_pending),
            ),
            date_text=ref_date,
        )

        live_reference = not historical_mode and is_trading_session(current_time)
        stage = "tick"
        tick_started = time.time()
        all_ticks = get_realtime_ticks(ContextInfo) if not historical_mode else {}
        tick_seconds = time.time() - tick_started
        if live_reference:
            actual_tick_date = latest_tick_trade_date({
                stock: all_ticks.get(stock, {}) for stock in g_stock_pool
            })
            if actual_tick_date and actual_tick_date != ref_date:
                log(
                    "[DATE] system reference=%s, QMT tick date=%s; use tick date"
                    % (ref_date, actual_tick_date),
                    "WARN",
                    actual_tick_date,
                )
                ref_date = actual_tick_date
                live_reference = False
        g_reference_date = ref_date
        pz1_cache_started = time.time()
        pz1_candidate_stocks = ensure_pz1_candidate_cache(ref_date)
        pz1_cache_seconds = time.time() - pz1_cache_started
        qy2_cache_started = time.time()
        qy2_candidate_stocks = ensure_qy2_candidate_cache(ref_date)
        qy2_cache_seconds = time.time() - qy2_cache_started
        write_status(ref_date, {
            "status": "running",
            "started_at": snapshot_at,
            "cycle_count": g_scan_count,
            "busy_skip_count": g_busy_skip_count,
        })

        stage = "filter"
        filter_started = time.time()
        ensure_history_publish_state(ref_date)
        ensure_qy1_history_publish_state(ref_date)
        ensure_qy2_history_publish_state(ref_date)
        kline_payload_cache = {}
        selected = []
        pz1_selected = []
        qy1_selected = []
        qy2_selected = []
        history_payloads = {}
        realtime_payloads = {}
        qy1_history_payloads = {}
        qy1_realtime_payloads = {}
        qy2_history_payloads = {}
        qy2_realtime_payloads = {}
        gain_candidate_count = 0
        no_tick_count = 0
        no_history_count = 0
        no_reference_date_count = 0
        history_fallback_count = 0
        history_fallback_sample = []
        stale_previous_close_count = 0
        stale_previous_close_sample = []
        ensure_dingtalk_state(ref_date)

        for stock in g_stock_pool:
            name = g_stock_names.get(stock, simple_code(stock))
            if is_risk_stock_name(name):
                continue
            history = g_history_data.get(stock)
            if not history:
                no_history_count += 1
                continue
            tick = all_ticks.get(stock) if live_reference else {}
            gain_gate_met = False
            pz1_candidate_met = stock in pz1_candidate_stocks
            qy2_candidate_met = stock in qy2_candidate_stocks
            if live_reference:
                if not isinstance(tick, dict) or not tick:
                    no_tick_count += 1
                    continue
                history_close, older_history_close = previous_history_closes(
                    history, ref_date
                )
                previous_close, previous_close_source = previous_close_from_tick(
                    tick, history_close, older_history_close
                )
                if previous_close_source == "history_stale_tick":
                    stale_previous_close_count += 1
                    if len(stale_previous_close_sample) < 5:
                        stale_previous_close_sample.append(
                            "%s lastClose=%s preClose=%s verified=%.4f"
                            % (stock, tick.get("lastClose"), tick.get("preClose"), previous_close)
                        )
                gain_gate_met = passes_realtime_gain_gate(
                    tick, MIN_REALTIME_GAIN_PCT, previous_close=previous_close
                )
                if not (gain_gate_met or pz1_candidate_met or qy2_candidate_met):
                    continue
                series = build_scan_reference_series(stock, history, tick, ref_date, realtime=True)
            else:
                series = build_scan_reference_series(stock, history, {}, ref_date, realtime=False)
                if ref_date not in (series.get("dates") or []):
                    if historical_mode:
                        no_reference_date_count += 1
                        continue
                    fallback_tick = all_ticks.get(stock, {})
                    series = build_scan_reference_series(stock, history, fallback_tick, ref_date, realtime=True)
                if ref_date not in (series.get("dates") or []):
                    no_reference_date_count += 1
                    continue
                tick = synthetic_tick_from_series(series)
                gain_gate_met = passes_realtime_gain_gate(
                    tick, MIN_REALTIME_GAIN_PCT
                )

            if live_reference and series.get("_pre_close_source") == "history":
                history_fallback_count += 1
                if len(history_fallback_sample) < 10:
                    history_fallback_sample.append(stock)

            metrics = None
            shared = {}
            if pz1_candidate_met:
                metrics = calculate_scan_window_metrics(
                    stock, history, series, ref_date, shared=shared
                )
                pz1_result = build_pz1_result(
                    stock, name, series, ref_date, snapshot_at, metrics=metrics, shared=shared
                )
                if pz1_result:
                    pz1_selected.append(pz1_result)
                    enqueue_stock_alert(pz1_result, ref_date)

            if qy2_candidate_met:
                qy2_result = build_qy2_result(
                    stock, name, series, ref_date, snapshot_at, metrics=metrics, shared=shared
                )
                if qy2_result:
                    qy2_selected.append(qy2_result)
                    qy2_history_payload, qy2_realtime_payload = build_kline_payloads(
                        stock, name, series, ref_date, snapshot_at,
                        published_codes=g_qy2_history_published_codes,
                        payload_cache=kline_payload_cache
                    )
                    qy2_history_payloads[qy2_result["code"]] = qy2_history_payload
                    qy2_realtime_payloads[qy2_result["code"]] = qy2_realtime_payload

            if not gain_gate_met:
                continue
            gain_candidate_count += 1

            if metrics is None:
                metrics = shared.get("window_metrics")
                if metrics is None:
                    metrics = calculate_scan_window_metrics(stock, history, series, ref_date, shared=shared)
            qy1_result = build_qy1_result(
                stock, name, series, ref_date, snapshot_at, metrics=metrics, shared=shared
            )
            if qy1_result:
                qy1_selected.append(qy1_result)
                qy1_history_payload, qy1_realtime_payload = build_kline_payloads(
                    stock, name, series, ref_date, snapshot_at,
                    published_codes=g_qy1_history_published_codes,
                    payload_cache=kline_payload_cache
                )
                qy1_history_payloads[qy1_result["code"]] = qy1_history_payload
                qy1_realtime_payloads[qy1_result["code"]] = qy1_realtime_payload

            result, history_payload, realtime_payload = evaluate_candidate(
                stock, name, series, tick, ref_date, snapshot_at, metrics=metrics,
                published_codes=g_history_published_codes,
                payload_cache=kline_payload_cache, shared=shared
            )
            if not result:
                continue
            selected.append(result)
            history_payloads[result["code"]] = history_payload
            realtime_payloads[result["code"]] = realtime_payload

        filter_seconds = time.time() - filter_started
        selected.sort(key=lambda item: (-safe_float(item.get("ref_gain")), item.get("code", "")))
        pz1_selected.sort(
            key=lambda item: (
                int(item.get("yin_count_20", 0) or 0),
                -safe_float(item.get("ref_gain")),
                item.get("code", ""),
            )
        )
        qy1_selected.sort(
            key=lambda item: (
                -int(item.get("strong_k_count_10d", 0) or 0),
                -safe_float(item.get("ref_gain")),
                item.get("code", ""),
            )
        )
        qy2_selected.sort(
            key=lambda item: (
                -int(item.get("strong_k_count_10d", 0) or 0),
                -safe_float(item.get("ref_gain")),
                item.get("code", ""),
            )
        )
        pz1_yin20_lte4_count = sum(
            int(item.get("pz1_yin_count_20_lte_4", 0) or 0)
            for item in pz1_selected
        )
        pz1_yin20_lte7_ref_yang_count = sum(
            int(item.get("pz1_yin_count_20_lte_7_and_ref_yang", 0) or 0)
            for item in pz1_selected
        )
        pz1_quanyang_10d_count = sum(
            int(item.get("pz1_quanyang_10d_met", 0) or 0)
            for item in pz1_selected
        )
        pz1_five_pull_count = sum(
            int(item.get("pz1_five_pull_met", 0) or 0)
            for item in pz1_selected
        )
        condition_summary = summarize_condition_hits(selected)
        five_pull_summary = summarize_five_pull_group_hits(selected)
        secondary_rejected_count = gain_candidate_count - len(selected)
        log(
            "[FILTER] gain5=%d secondary_rejected=%d selected=%d qy1=%d qy2=%d "
            "no_tick=%d no_history=%d no_ref_date=%d"
            % (
                gain_candidate_count,
                secondary_rejected_count,
                len(selected),
                len(qy1_selected),
                len(qy2_selected),
                no_tick_count,
                no_history_count,
                no_reference_date_count,
            ),
            date_text=ref_date,
        )
        if history_fallback_count:
            log(
                "[PRE-CLOSE] history fallback count=%d sample=%s"
                % (
                    history_fallback_count,
                    ",".join(history_fallback_sample),
                ),
                "WARN",
                ref_date,
            )
        if stale_previous_close_count:
            log(
                "[PRE-CLOSE] stale tick corrected count=%d sample=%s"
                % (stale_previous_close_count, "; ".join(stale_previous_close_sample)),
                "WARN",
                ref_date,
            )
        log(
            "[CONDITION] C2_yin20_lte7=%d C3_high5_and_close_above_ma10=%d "
            "C4_low_above_ma5_gte5=%d multi=%d"
            % (
                condition_summary["condition_2_count"],
                condition_summary["condition_3_count"],
                condition_summary["condition_4_count"],
                condition_summary["multi_condition_count"],
            ),
            date_text=ref_date,
        )
        log(
            "[5PULL-GROUP] yin_up=%d cross_star=%d next_pull=%d yin_eaten=%d"
            % (
                five_pull_summary["five_pull_all_yin_up_count"],
                five_pull_summary["five_pull_all_yin_cross_star_count"],
                five_pull_summary["five_pull_yin_pull_next_count"],
                five_pull_summary["five_pull_all_yin_eaten_next_count"],
            ),
            date_text=ref_date,
        )
        if selected and LOG_SELECTED_LIMIT > 0:
            top_rows = []
            for item in selected[:LOG_SELECTED_LIMIT]:
                top_rows.append(
                    "%s/%s %.2f%% C%d%d%d"
                    % (
                        item.get("code", ""),
                        item.get("name", ""),
                        safe_float(item.get("ref_gain")),
                        int(item.get("condition_2_met", 0)),
                        int(item.get("condition_3_met", 0)),
                        int(item.get("condition_4_met", 0)),
                    )
                )
            log(
                "[SELECTED-TOP] %s%s"
                % (
                    "; ".join(top_rows),
                    " ..." if len(selected) > LOG_SELECTED_LIMIT else "",
                ),
                date_text=ref_date,
            )

        scan_info = {
            "status": "publishing",
            "started_at": snapshot_at,
            "snapshot_at": snapshot_at,
            "cycle_count": g_scan_count,
            "scanned_count": len(g_stock_pool),
            "gain_candidate_count": gain_candidate_count,
            "selected_count": len(selected),
            "pz1_selected_count": len(pz1_selected),
            "qy1_selected_count": len(qy1_selected),
            "qy2_selected_count": len(qy2_selected),
            "pz1_yin20_lte4_count": pz1_yin20_lte4_count,
            "pz1_yin20_lte7_ref_yang_count": pz1_yin20_lte7_ref_yang_count,
            "pz1_quanyang_10d_count": pz1_quanyang_10d_count,
            "pz1_five_pull_count": pz1_five_pull_count,
            "no_tick_count": no_tick_count,
            "no_history_count": no_history_count,
            "no_reference_date_count": no_reference_date_count,
            "history_fallback_count": history_fallback_count,
            "history_fallback_sample": history_fallback_sample,
            "secondary_rejected_count": secondary_rejected_count,
            "condition_2_count": condition_summary["condition_2_count"],
            "condition_3_count": condition_summary["condition_3_count"],
            "condition_4_count": condition_summary["condition_4_count"],
            "multi_condition_count": condition_summary["multi_condition_count"],
            "five_pull_all_yin_up_count": five_pull_summary["five_pull_all_yin_up_count"],
            "five_pull_all_yin_cross_star_count": five_pull_summary["five_pull_all_yin_cross_star_count"],
            "five_pull_yin_pull_next_count": five_pull_summary["five_pull_yin_pull_next_count"],
            "five_pull_all_yin_eaten_next_count": five_pull_summary["five_pull_all_yin_eaten_next_count"],
            "actual_interval_seconds": round(actual_interval_seconds, 4),
            "dingtalk_queue_size": g_dingtalk_queue.qsize(),
            "dingtalk_pending_count": len(g_dingtalk_pending),
            "tick_seconds": round(tick_seconds, 4),
            "pz1_cache_seconds": round(pz1_cache_seconds, 4),
            "pz1_candidate_count": len(pz1_candidate_stocks),
            "qy2_cache_seconds": round(qy2_cache_seconds, 4),
            "qy2_candidate_count": len(qy2_candidate_stocks),
            "filter_seconds": round(filter_seconds, 4),
        }

        stage = "pz_redis"
        pz_redis_seconds = publish_pz_snapshot(ref_date, selected, scan_info)
        stage = "kline_history"
        kline_history_seconds, history_written = publish_kline_history(ref_date, history_payloads)
        stage = "kline_realtime"
        kline_realtime_seconds = publish_kline_realtime(ref_date, realtime_payloads, snapshot_at)
        stage = "pz1_redis"
        pz1_scan_info = dict(scan_info)
        pz1_scan_info.update({
            "selected_count": len(pz1_selected),
            "pz_selected_count": len(selected),
            "selection_rule": "yin_count_20_lte_7_and_ref_yang_or_quanyang_10d_or_high_gain_10d_gte_5_or_high_gain_prev3_short_shadow",
            "kline_published": False,
        })
        pz1_redis_seconds = publish_pz1_snapshot(
            ref_date, pz1_selected, pz1_scan_info
        )
        stage = "qy1_redis"
        qy1_scan_info = dict(scan_info)
        qy1_scan_info.update({
            "selected_count": len(qy1_selected),
            "pz_selected_count": len(selected),
            "selection_rule": "gain_gte_5_and_all_yin_no_touch_or_eaten_10d",
            "kline_published": True,
        })
        qy1_redis_seconds = publish_qy1_snapshot(
            ref_date, qy1_selected, qy1_scan_info
        )
        stage = "qy1_kline_history"
        qy1_kline_history_seconds, qy1_history_written = publish_qy1_kline_history(
            ref_date, qy1_history_payloads
        )
        stage = "qy1_kline_realtime"
        qy1_kline_realtime_seconds = publish_qy1_kline_realtime(
            ref_date, qy1_realtime_payloads, snapshot_at
        )
        stage = "qy2_redis"
        qy2_scan_info = dict(scan_info)
        qy2_scan_info.update({
            "selected_count": len(qy2_selected),
            "pz_selected_count": len(selected),
            "selection_rule": "all_yin_low_gte_ma5_or_eaten_10d",
            "kline_published": True,
        })
        qy2_redis_seconds = publish_qy2_snapshot(
            ref_date, qy2_selected, qy2_scan_info
        )
        stage = "qy2_kline_history"
        qy2_kline_history_seconds, qy2_history_written = publish_qy2_kline_history(
            ref_date, qy2_history_payloads
        )
        stage = "qy2_kline_realtime"
        qy2_kline_realtime_seconds = publish_qy2_kline_realtime(
            ref_date, qy2_realtime_payloads, snapshot_at
        )
        log(
            "[REDIS] pz=%s stocks=%d; history=%s new=%d; realtime=%s stocks=%d; "
            "pz1=%s stocks=%d five_pull=%d; qy1=%s stocks=%d; "
            "qy1_history=%s new=%d; qy1_realtime=%s stocks=%d; "
            "qy2=%s stocks=%d; qy2_history=%s new=%d; qy2_realtime=%s stocks=%d"
            % (
                redis_key(REDIS_KEY_PREFIX, ref_date),
                len(selected),
                redis_key(KLINE_HISTORY_PREFIX, ref_date),
                history_written,
                redis_key(KLINE_REALTIME_PREFIX, ref_date),
                len(realtime_payloads),
                redis_key(PZ1_REDIS_KEY_PREFIX, ref_date),
                len(pz1_selected),
                pz1_five_pull_count,
                redis_key(QY1_REDIS_KEY_PREFIX, ref_date),
                len(qy1_selected),
                redis_key(QY1_KLINE_HISTORY_PREFIX, ref_date),
                qy1_history_written,
                redis_key(QY1_KLINE_REALTIME_PREFIX, ref_date),
                len(qy1_realtime_payloads),
                redis_key(QY2_REDIS_KEY_PREFIX, ref_date),
                len(qy2_selected),
                redis_key(QY2_KLINE_HISTORY_PREFIX, ref_date),
                qy2_history_written,
                redis_key(QY2_KLINE_REALTIME_PREFIX, ref_date),
                len(qy2_realtime_payloads),
            ),
            date_text=ref_date,
        )
        ladder_seconds = publish_limit_up_ladder(ContextInfo, all_ticks, ref_date, snapshot_at)
        total_seconds = time.time() - started
        finished_at = now_text()
        final_info = dict(scan_info)
        final_info.update({
            "status": "success",
            "finished_at": finished_at,
            "published_at": finished_at,
            "history_written": history_written,
            "pz_redis_seconds": round(pz_redis_seconds, 4),
            "pz1_redis_seconds": round(pz1_redis_seconds, 4),
            "qy1_redis_seconds": round(qy1_redis_seconds, 4),
            "qy2_redis_seconds": round(qy2_redis_seconds, 4),
            "ladder_seconds": round(ladder_seconds, 4),
            "kline_history_seconds": round(kline_history_seconds, 4),
            "kline_realtime_seconds": round(kline_realtime_seconds, 4),
            "qy1_history_written": qy1_history_written,
            "qy1_kline_history_seconds": round(qy1_kline_history_seconds, 4),
            "qy1_kline_realtime_seconds": round(qy1_kline_realtime_seconds, 4),
            "qy2_history_written": qy2_history_written,
            "qy2_kline_history_seconds": round(qy2_kline_history_seconds, 4),
            "qy2_kline_realtime_seconds": round(qy2_kline_realtime_seconds, 4),
            "total_seconds": round(total_seconds, 4),
        })
        final_pipe = g_redis_client.pipeline(transaction=False)
        final_pipe.hset(
            redis_key(REDIS_KEY_PREFIX, ref_date),
            "_scan_info",
            encode_payload(final_info),
        )
        pz1_final_info = dict(final_info)
        pz1_final_info.update({
            "selected_count": len(pz1_selected),
            "pz_selected_count": len(selected),
            "selection_rule": "yin_count_20_lte_7_and_ref_yang_or_quanyang_10d_or_high_gain_10d_gte_5_or_high_gain_prev3_short_shadow",
            "kline_published": False,
        })
        final_pipe.hset(
            redis_key(PZ1_REDIS_KEY_PREFIX, ref_date),
            "_scan_info",
            encode_payload(pz1_final_info),
        )
        qy1_final_info = dict(final_info)
        qy1_final_info.update({
            "selected_count": len(qy1_selected),
            "pz_selected_count": len(selected),
            "selection_rule": "gain_gte_5_and_all_yin_no_touch_or_eaten_10d",
            "kline_published": True,
            "history_written": qy1_history_written,
            "kline_history_seconds": round(qy1_kline_history_seconds, 4),
            "kline_realtime_seconds": round(qy1_kline_realtime_seconds, 4),
        })
        final_pipe.hset(
            redis_key(QY1_REDIS_KEY_PREFIX, ref_date),
            "_scan_info",
            encode_payload(qy1_final_info),
        )
        qy2_final_info = dict(final_info)
        qy2_final_info.update({
            "selected_count": len(qy2_selected),
            "pz_selected_count": len(selected),
            "selection_rule": "all_yin_low_gte_ma5_or_eaten_10d",
            "kline_published": True,
            "history_written": qy2_history_written,
            "kline_history_seconds": round(qy2_kline_history_seconds, 4),
            "kline_realtime_seconds": round(qy2_kline_realtime_seconds, 4),
        })
        final_pipe.hset(
            redis_key(QY2_REDIS_KEY_PREFIX, ref_date),
            "_scan_info",
            encode_payload(qy2_final_info),
        )
        write_status(ref_date, final_info, pipeline=final_pipe)
        final_pipe.execute()
        try:
            publish_performance_record(ref_date, final_info)
        except Exception as perf_error:
            log(
                "[PERF] Redis record failed: %s" % perf_error,
                "WARN",
                ref_date,
            )
        log(
            "[CYCLE] date=%s cycle=%d interval=%.3fs market=%d gain5=%d selected=%d "
            "pz1=%d qy1=%d qy2=%d tick=%.3fs filter=%.3fs pz=%.3fs pz1_redis=%.3fs "
            "qy1_redis=%.3fs qy2_redis=%.3fs "
            "history=%.3fs realtime=%.3fs total=%.3fs"
            % (
                ref_date,
                g_scan_count,
                actual_interval_seconds,
                len(g_stock_pool),
                gain_candidate_count,
                len(selected),
                len(pz1_selected),
                len(qy1_selected),
                len(qy2_selected),
                tick_seconds,
                filter_seconds,
                pz_redis_seconds,
                pz1_redis_seconds,
                qy1_redis_seconds,
                qy2_redis_seconds,
                kline_history_seconds,
                kline_realtime_seconds,
                total_seconds,
            ),
            "WARN" if total_seconds > 5.0 else "INFO",
            ref_date,
        )
        if tick_seconds > 1.0:
            log("[PERF] QMT snapshot exceeds 1 second", "WARN", ref_date)
        if filter_seconds > 2.0:
            log("[PERF] candidate calculation exceeds 2 seconds", "WARN", ref_date)
        if pz_redis_seconds > 2.0:
            log("[PERF] PZ Redis publish exceeds 2 seconds", "WARN", ref_date)
        if pz1_redis_seconds > 2.0:
            log("[PERF] PZ1 Redis publish exceeds 2 seconds", "WARN", ref_date)
        if qy1_redis_seconds > 2.0:
            log("[PERF] QY1 Redis publish exceeds 2 seconds", "WARN", ref_date)
        if qy2_redis_seconds > 2.0:
            log("[PERF] QY2 Redis publish exceeds 2 seconds", "WARN", ref_date)
        if kline_history_seconds + kline_realtime_seconds > 2.0:
            log("[PERF] K-line Redis publish exceeds 2 seconds", "WARN", ref_date)
        if qy2_kline_history_seconds + qy2_kline_realtime_seconds > 2.0:
            log("[PERF] QY2 K-line publish exceeds 2 seconds", "WARN", ref_date)
        if total_seconds > 5.0:
            log("[PERF] total cycle exceeds 5 seconds", "WARN", ref_date)
        if historical_mode:
            g_historical_done = True
        return final_info
    except Exception as error:
        failed_info = {
            "status": "failed",
            "started_at": started_at,
            "failed_at": now_text(),
            "finished_at": now_text(),
            "failed_stage": stage,
            "error": "%s: %s" % (type(error).__name__, error),
            "cycle_count": g_scan_count,
            "actual_interval_seconds": round(actual_interval_seconds, 4),
            "scanned_count": len(g_stock_pool),
            "gain_candidate_count": int(locals().get("gain_candidate_count", 0)),
            "selected_count": len(locals().get("selected", [])),
            "pz1_selected_count": len(locals().get("pz1_selected", [])),
            "qy1_selected_count": len(locals().get("qy1_selected", [])),
            "qy2_selected_count": len(locals().get("qy2_selected", [])),
            "tick_seconds": round(locals().get("tick_seconds", 0.0), 4),
            "filter_seconds": round(locals().get("filter_seconds", 0.0), 4),
            "pz_redis_seconds": round(locals().get("pz_redis_seconds", 0.0), 4),
            "pz1_redis_seconds": round(locals().get("pz1_redis_seconds", 0.0), 4),
            "qy1_redis_seconds": round(locals().get("qy1_redis_seconds", 0.0), 4),
            "qy2_redis_seconds": round(locals().get("qy2_redis_seconds", 0.0), 4),
            "kline_history_seconds": round(locals().get("kline_history_seconds", 0.0), 4),
            "kline_realtime_seconds": round(locals().get("kline_realtime_seconds", 0.0), 4),
            "qy1_kline_history_seconds": round(locals().get("qy1_kline_history_seconds", 0.0), 4),
            "qy1_kline_realtime_seconds": round(locals().get("qy1_kline_realtime_seconds", 0.0), 4),
            "qy2_kline_history_seconds": round(locals().get("qy2_kline_history_seconds", 0.0), 4),
            "qy2_kline_realtime_seconds": round(locals().get("qy2_kline_realtime_seconds", 0.0), 4),
            "total_seconds": round(time.time() - started, 4),
        }
        log("[CYCLE] failed stage=%s error=%s\n%s" % (stage, error, traceback.format_exc()), "ERROR", ref_date)
        performance_date = ref_date or fallback_performance_date
        if performance_date:
            try:
                write_status(performance_date, failed_info)
            except Exception as status_error:
                log("[STATUS] failure record failed: %s" % status_error, "WARN", performance_date)
            try:
                publish_performance_record(performance_date, failed_info)
            except Exception as perf_error:
                log("[PERF] Redis failure record failed: %s" % perf_error, "WARN", performance_date)
        return None
    finally:
        g_scan_lock.release()


def reload_history_data(ContextInfo=None):
    ContextInfo = ContextInfo or g_context
    if ContextInfo is None:
        return
    if not g_scan_lock.acquire(blocking=False):
        log("[HISTORY] reload skipped because scan is running", "WARN")
        return
    try:
        preload_history_data(ContextInfo, normalize_trade_date(REFERENCE_DATE))
        init_ladder_market(ContextInfo, g_ladder_pool or g_stock_pool)
    finally:
        g_scan_lock.release()


# ==================== QMT entry points ====================

def init(ContextInfo):
    global g_context, g_initialized, g_stock_pool, g_stock_names, g_reference_date
    if g_initialized:
        return
    g_context = ContextInfo
    log("=" * 72)
    log("[START] PZ intraday scanner")
    log("[CONFIG] timer=%s check_trading_time=%s reference_date=%s" % (
        TIMER_INTERVAL_TEXT, CHECK_TRADING_TIME, REFERENCE_DATE or "realtime"
    ))
    log("[CONFIG] optimization=history_cache_shared_kline_pipeline_v1")
    log("[CONFIG] pricing=history_validated_previous_close_v2")
    log(
        "[CONFIG] pz_secondary=C2_OR_C3_OR_C4_OR_5pull_yin_up_"
        "cross_star_next_pull_yin_eaten"
    )
    log(
        "[CONFIG] log_enabled=%s dingtalk_routes="
        "ref_gain_gte5_AND_qiangchi->five_pull; "
        "other_conditions_only=disabled"
        % LOG_ENABLED
    )
    if not init_redis():
        return
    try:
        ContextInfo.subscribe_whole_quote(["SH", "SZ"])
        log("[QUOTE] subscribed SH and SZ")
    except Exception as error:
        log("[QUOTE] subscribe failed: %s" % error, "WARN")

    sectors = ("\u6caa\u6df1A\u80a1", "\u79d1\u521b\u677f", "\u5317\u4ea4\u6240A\u80a1")
    pool = []
    for sector in sectors:
        try:
            stocks = ContextInfo.get_stock_list_in_sector(sector) or []
            pool.extend(stocks)
            log("[MARKET] %s=%d" % (sector, len(stocks)))
        except Exception as error:
            log("[MARKET] %s failed: %s" % (sector, error), "WARN")
    g_stock_pool = sorted(set(pool))
    for stock in g_stock_pool:
        try:
            name = ContextInfo.get_stock_name(stock)
        except Exception:
            name = simple_code(stock)
        if name and not is_risk_stock_name(name):
            g_stock_names[stock] = name
    g_stock_pool = [stock for stock in g_stock_pool if stock in g_stock_names]
    log("[MARKET] usable=%d" % len(g_stock_pool))
    init_ladder_market(ContextInfo, pool)

    preload_history_data(ContextInfo, normalize_trade_date(REFERENCE_DATE))
    g_reference_date = resolve_reference_date()
    persist_thread = threading.Thread(target=dingtalk_persist_worker, daemon=True)
    persist_thread.start()
    ding_thread = threading.Thread(target=dingtalk_worker, daemon=True)
    ding_thread.start()
    log("[DINGTALK] sender and persist workers started")

    historical_mode = bool(normalize_trade_date(REFERENCE_DATE))
    if not historical_mode:
        try:
            ContextInfo.run_time("reload_history_data", "1nDay", "2026-01-01 09:15:00", "SH")
            ContextInfo.run_time("scan_stocks", TIMER_INTERVAL_TEXT, "2026-01-01 00:00:00", "SH")
            log("[TIMER] 09:15 history reload and 1-second scan driver registered")
        except Exception as error:
            log("[TIMER] registration failed: %s" % error, "ERROR")
            return

    initial_time = datetime.now()
    if should_run_initial_scan(initial_time, historical_mode=historical_mode):
        scan_stocks(ContextInfo, force=historical_mode)
    else:
        log(
            "[WAIT] initial scan skipped before trading session; "
            "timer will scan from 09:25"
        )
    g_initialized = True


def handlebar(ContextInfo):
    return
