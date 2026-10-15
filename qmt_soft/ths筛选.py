# -*- coding: gbk -*-
"""
QMT同花顺板块股票日K同步程序。

数据来源:
  - 股票池: Redis ths:latest 指向的 ths:YYYYMMDD
  - 行情: QMT日K及实时快照
  - 输出: Redis Hash qmt:kline:YYYYMMDD

实时模式每秒触发一次，只HSET清洗入选股票，不删除目标Hash中的其他字段。
REFERENCE_DATE填写YYYYMMDD时进入历史模式，只发布一次，不启动定时器。
"""

from __future__ import print_function

import csv
import json
import math
import os
import re
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

try:
    import redis
except Exception:
    redis = None


# ==================== 用户配置 ====================

REFERENCE_DATE = ""

REDIS_HOST = "175.24.178.113"
REDIS_PORT = 5268
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_DB = 0

THS_LATEST_KEY = "ths:latest"
THS_KEY_PREFIX = "ths"
THS_CLEANING_KEY_PREFIX = "thscleaning"
REDIS_QMT_KLINE_KEY_PREFIX = "qmt:kline"
REDIS_QMT_KLINE_DAYS = 60
REDIS_QMT_KLINE_TTL_SEC = 7 * 24 * 60 * 60
REDIS_STATUS_TTL_SEC = 7 * 24 * 60 * 60

HISTORY_BATCH_SIZE = 300
HISTORY_REQUEST_DAYS = REDIS_QMT_KLINE_DAYS + 5
PUBLISH_INTERVAL_TEXT = "1nSecond"
CLOSE_BAR_REFRESH_SEC = 20

LOG_BASE_DIR = r"C:\Users\lyy\Desktop\股票\预警"
STRATEGY_DIR_NAME = "K线入Redis-同花顺板块"


# ==================== 运行状态 ====================

g_context = None
g_redis_client = None
g_initialized = False
g_cycle_lock = threading.Lock()
g_log_lock = threading.Lock()

g_market_code_map = {}
g_stock_names = {}
g_stock_pool = []
g_history_data = {}
g_ths_source_key = ""
g_ths_stats = {}
g_ths_stock_metadata = {}
g_last_reload_date = ""
g_busy_skip_count = 0
g_cycle_count = 0
g_recent_metrics = deque(maxlen=10)
g_consecutive_slow_cycles = 0
g_last_cleaning_signature = ""

g_close_bar_cache_date = ""
g_close_bar_cache = {}
g_close_bar_last_fetch_at = 0.0
g_history_rows_cache_date = ""
g_history_rows_cache = {}


# ==================== 通用工具 ====================

def _now_text(now=None):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _compact_date(value):
    if isinstance(value, str) and len(value) == 8 and value.isdecimal():
        return value
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:8] if len(digits) >= 8 else ""


def _simple_code(stock):
    text = str(stock or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    match = re.search(r"(\d{6})", text)
    return match.group(1) if match else ""


def _float(value, default=0.0):
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return float(default)
        return result
    except Exception:
        return float(default)


def _optional_float(value):
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except Exception:
        return None


def _date_label(trade_date):
    text = str(trade_date or "")
    if len(text) == 8:
        return "%s/%s" % (text[4:6], text[6:8])
    return text


def _is_valid_date(value):
    return _is_valid_compact_date(_compact_date(value))


@lru_cache(maxsize=4096)
def _is_valid_compact_date(text):
    if len(text) != 8:
        return False
    try:
        datetime.strptime(text, "%Y%m%d")
        return True
    except Exception:
        return False


def _infer_market_stock(code):
    code = _simple_code(code)
    if not code:
        return ""
    if code.startswith(("4", "8", "92")):
        return code + ".BJ"
    if code.startswith(("5", "6", "9")):
        return code + ".SH"
    if code.startswith(("0", "1", "2", "3")):
        return code + ".SZ"
    return ""


def is_excluded_stock_name(name):
    text = str(name or "").strip()
    upper = text.upper()
    return bool(text) and ("退" in text or "退市" in text or "ST" in upper)


def get_log_dir(date_text=None):
    compact = _compact_date(date_text) or datetime.now().strftime("%Y%m%d")
    path = os.path.join(LOG_BASE_DIR, compact, STRATEGY_DIR_NAME)
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def log(message, level="INFO", date_text=None):
    line = "[%s] [%s] %s" % (_now_text(), level, str(message))
    with g_log_lock:
        try:
            print(line)
        except Exception:
            pass
        try:
            path = os.path.join(get_log_dir(date_text), "运行日志.log")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass


# ==================== THS股票池 ====================

def _decode_ths_payload(raw):
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    try:
        payload = json.loads(str(raw))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _valid_ths_payload(payload):
    if not isinstance(payload, dict):
        return False
    return isinstance(payload.get("stocks"), list) or isinstance(payload.get("boards_summary"), list)


def _redis_keys(redis_client, pattern):
    if hasattr(redis_client, "scan_iter"):
        try:
            return list(redis_client.scan_iter(match=pattern, count=200))
        except Exception:
            pass
    return list(redis_client.keys(pattern))


def _redis_get_payload_unchecked(redis_client, key):
    """
    QMT内置redis-py会把包含股票代码和普通数字的响应误判为敏感数据。
    这里只对THS大JSON使用底层连接读取，绕过客户端响应文本扫描；Redis命令仍是只读GET。
    """
    pool = getattr(redis_client, "connection_pool", None)
    if pool is None:
        return redis_client.get(key)

    connection = None
    try:
        try:
            connection = pool.get_connection("GET")
        except TypeError:
            connection = pool.get_connection()
        connection.send_command("GET", key)
        return connection.read_response()
    except Exception:
        if connection is not None:
            try:
                connection.disconnect()
            except Exception:
                pass
        raise
    finally:
        if connection is not None:
            try:
                pool.release(connection)
            except Exception:
                pass


def resolve_ths_source(redis_client, reference_date=""):
    """返回可用的(ths key, payload)，历史模式不读取参考日之后的数据。"""
    ref = _compact_date(reference_date)
    candidates = []
    first = ""

    if not ref:
        latest = redis_client.get(THS_LATEST_KEY)
        if isinstance(latest, bytes):
            latest = latest.decode("utf-8", errors="ignore")
        latest = str(latest or "").strip()
        if re.match(r"^ths:\d{8}$", latest):
            first = latest
            payload = _decode_ths_payload(_redis_get_payload_unchecked(redis_client, latest))
            if _valid_ths_payload(payload):
                return latest, payload

    for raw_key in _redis_keys(redis_client, "ths:*"):
        if isinstance(raw_key, bytes):
            raw_key = raw_key.decode("utf-8", errors="ignore")
        key = str(raw_key)
        match = re.match(r"^ths:(\d{8})$", key)
        if not match:
            continue
        if ref and match.group(1) > ref:
            continue
        candidates.append(key)

    ordered = []
    seen = set()
    if candidates:
        dated = sorted(set(candidates), key=lambda item: item.rsplit(":", 1)[-1], reverse=True)
        # An invalid latest payload was already read; fall back to dated keys.
        if first:
            seen.add(first)
        ordered.extend([key for key in dated if key not in ordered])

    for key in ordered:
        if key in seen:
            continue
        seen.add(key)
        payload = _decode_ths_payload(_redis_get_payload_unchecked(redis_client, key))
        if _valid_ths_payload(payload):
            return key, payload
    raise RuntimeError("没有找到可用的THS Redis快照")


def _ths_stock_relations(payload):
    stocks = list(payload.get("stocks") or [])
    if stocks:
        return stocks
    result = []
    for summary in payload.get("boards_summary") or []:
        board_name = str((summary or {}).get("board_name") or "")
        for item in (summary or {}).get("stocks") or []:
            stock = dict(item or {})
            stock.setdefault("board_name", board_name)
            result.append(stock)
    return result


def _optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _ths_board_limit_up_counts(payload):
    """按板块名称提取THS boards里的涨停家数。"""
    result = {}
    for key in ("boards", "boards_summary"):
        for board in payload.get(key) or []:
            board = board or {}
            board_name = str(board.get("board_name") or "").strip()
            limit_up_count = _optional_int(board.get("limit_up_count"))
            if board_name and limit_up_count is not None and board_name not in result:
                result[board_name] = max(0, limit_up_count)
    return result


def collect_ths_stock_metadata(payload):
    board_limit_up_counts = _ths_board_limit_up_counts(payload)
    grouped = {}
    for item in _ths_stock_relations(payload):
        item = item or {}
        code = _simple_code(item.get("code"))
        if not code:
            continue
        entry = grouped.setdefault(code, {
            "code": code,
            "source_name": str(item.get("name") or code),
            "board_names": [],
            "board_limit_up_counts": {},
            "limit_up_count": 0,
            "ths_change_percent": None,
            "ths_price": 0.0,
            "ths_snapshot_time": "",
        })
        board_name = str(item.get("board_name") or "").strip()
        if board_name and board_name not in entry["board_names"]:
            entry["board_names"].append(board_name)
        limit_up_count = _optional_int(item.get("limit_up_count"))
        if limit_up_count is None and board_name:
            limit_up_count = board_limit_up_counts.get(board_name)
        if board_name and limit_up_count is not None:
            entry["board_limit_up_counts"][board_name] = max(0, limit_up_count)
        change = _optional_float(item.get("change_percent"))
        if change is not None:
            current = entry["ths_change_percent"]
            entry["ths_change_percent"] = change if current is None else max(current, change)
        snapshot_time = str(item.get("snapshot_time") or "")
        if snapshot_time >= entry["ths_snapshot_time"]:
            entry["ths_snapshot_time"] = snapshot_time
            entry["ths_price"] = _float(item.get("price"), 0.0)
            entry["source_name"] = str(item.get("name") or entry["source_name"])
    for entry in grouped.values():
        entry["board_names"].sort()
        entry["board_limit_up_counts"] = {
            board_name: entry["board_limit_up_counts"][board_name]
            for board_name in entry["board_names"]
            if board_name in entry["board_limit_up_counts"]
        }
        if entry["board_limit_up_counts"]:
            entry["limit_up_count"] = max(entry["board_limit_up_counts"].values())
    return grouped


def select_ths_universe(payload, market_code_map, qmt_names):
    relations = _ths_stock_relations(payload)
    source_names = {}
    invalid_count = 0
    for item in relations:
        code = _simple_code((item or {}).get("code"))
        if not code:
            invalid_count += 1
            continue
        source_names.setdefault(code, str((item or {}).get("name") or code))

    stocks = []
    names = {}
    excluded = 0
    unmapped = 0
    for code in sorted(source_names):
        stock = market_code_map.get(code) or _infer_market_stock(code)
        if not stock:
            unmapped += 1
            continue
        name = str(qmt_names.get(stock) or source_names.get(code) or code)
        if is_excluded_stock_name(name):
            excluded += 1
            continue
        stocks.append(stock)
        names[stock] = name

    stats = {
        "source_relations": len(relations),
        "unique_codes": len(source_names),
        "selected_count": len(stocks),
        "excluded_risk": excluded,
        "invalid_code": invalid_count,
        "unmapped_code": unmapped,
    }
    return stocks, names, stats


# ==================== 时间与日期 ====================

def is_trading_session(current_time):
    if current_time.weekday() >= 5:
        return False
    current = current_time.time()
    morning_start = current_time.replace(hour=9, minute=25, second=0, microsecond=0).time()
    morning_end = current_time.replace(hour=11, minute=30, second=0, microsecond=0).time()
    afternoon_start = current_time.replace(hour=13, minute=0, second=0, microsecond=0).time()
    afternoon_end = current_time.replace(hour=15, minute=5, second=0, microsecond=0).time()
    return morning_start <= current <= morning_end or afternoon_start <= current <= afternoon_end


def should_reload_history(current_time, last_reload_date):
    if current_time.weekday() >= 5:
        return False
    if current_time.strftime("%H%M%S") < "092500":
        return False
    return str(last_reload_date or "") != current_time.strftime("%Y%m%d")


def is_confirmed_current_trade_day(current_time, resolved_trade_date):
    """实时发布只接受QMT确认到今天的数据，避免节假日污染上一交易日。"""
    if current_time.weekday() >= 5:
        return False
    return _compact_date(resolved_trade_date) == current_time.strftime("%Y%m%d")


def should_request_realtime_ticks(current_time, latest_history_date,
                                  allow_latest_trade_date=False):
    """盘外单次运行跨日时只用历史K，避免旧快照覆盖最近交易日。"""
    if not allow_latest_trade_date:
        return True
    return _compact_date(latest_history_date) == current_time.strftime("%Y%m%d")


def _tick_trade_date(tick):
    if not isinstance(tick, dict):
        return ""
    for field in ("time", "timestamp", "timetag", "datetime"):
        raw = tick.get(field)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 13 and len(digits) <= 16:
            try:
                value = float(digits[:13]) / 1000.0
                return datetime.fromtimestamp(value).strftime("%Y%m%d")
            except Exception:
                pass
        if len(digits) >= 10 and len(digits) < 13:
            try:
                return datetime.fromtimestamp(float(digits[:10])).strftime("%Y%m%d")
            except Exception:
                pass
        if len(digits) >= 8 and _is_valid_date(digits[:8]):
            return digits[:8]
    return ""


def resolve_qmt_trade_date(histories, ticks=None, current_time=None, reference_date=""):
    current_time = current_time or datetime.now()
    requested = _compact_date(reference_date)
    unique_dates = {
        _compact_date(value)
        for history in (histories or {}).values()
        for value in (history or {}).get("dates") or []
    }
    dates = {value for value in unique_dates
             if _is_valid_date(value) and (not requested or value <= requested)}
    latest_history = max(dates) if dates else ""

    if requested:
        return requested if requested in dates else latest_history

    today = current_time.strftime("%Y%m%d")
    if current_time.weekday() < 5:
        if latest_history == today:
            return today
        if is_trading_session(current_time):
            for tick in (ticks or {}).values():
                if _tick_trade_date(tick) == today:
                    return today
    return latest_history


# ==================== QMT日K处理 ====================

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


def filter_suspended_days(stock_data):
    if stock_data is None or len(stock_data) == 0:
        return stock_data
    valid = []
    previous_close = None
    for index in range(len(stock_data)):
        open_price = _float(stock_data["open"].iloc[index])
        high_price = _float(stock_data["high"].iloc[index])
        low_price = _float(stock_data["low"].iloc[index])
        close_price = _float(stock_data["close"].iloc[index])
        suspended = min(open_price, high_price, low_price, close_price) <= 0
        if (not suspended and previous_close is not None and
                open_price == high_price == low_price == close_price and
                abs(close_price - previous_close) < 0.01):
            suspended = True
        valid.append(not suspended)
        if not suspended:
            previous_close = close_price
    return stock_data[valid].copy()


def _index_dates(stock_data):
    result = []
    for value in list(stock_data.index):
        try:
            text = value.strftime("%Y%m%d")
        except Exception:
            text = _compact_date(value)
        result.append(text)
    return result


def _series_values(stock_data, field, size):
    try:
        if field in stock_data:
            return [_float(value) for value in list(stock_data[field].values)]
    except Exception:
        pass
    return [0.0] * size


def _history_from_frame(stock_data, end_date=""):
    if stock_data is None or len(stock_data) == 0:
        return None
    stock_data = filter_suspended_days(stock_data)
    if stock_data is None or len(stock_data) == 0:
        return None
    dates = _index_dates(stock_data)
    requested = _compact_date(end_date)
    indexes = [index for index, value in enumerate(dates)
               if _is_valid_date(value) and (not requested or value <= requested)]
    if not indexes:
        return None
    last = indexes[-1] + 1
    stock_data = stock_data.iloc[:last]
    dates = dates[:last]
    size = len(stock_data)
    return {
        "dates": dates,
        "opens": _series_values(stock_data, "open", size),
        "highs": _series_values(stock_data, "high", size),
        "lows": _series_values(stock_data, "low", size),
        "closes": _series_values(stock_data, "close", size),
        "volumes": _series_values(stock_data, "volume", size),
        "amounts": _series_values(stock_data, "amount", size),
    }


def load_history_for_stocks(ContextInfo, stocks, end_date=""):
    stocks = list(stocks or [])
    loaded = {}
    failures = {}
    if not stocks:
        return loaded, failures

    requested = _compact_date(end_date)
    if requested:
        api_end = (datetime.strptime(requested, "%Y%m%d") + timedelta(days=7)).strftime("%Y%m%d")
    else:
        api_end = (datetime.now() + timedelta(days=7)).strftime("%Y%m%d")

    total_batches = int(math.ceil(len(stocks) / float(HISTORY_BATCH_SIZE)))
    for offset in range(0, len(stocks), HISTORY_BATCH_SIZE):
        batch = stocks[offset:offset + HISTORY_BATCH_SIZE]
        batch_number = offset // HISTORY_BATCH_SIZE + 1
        try:
            try:
                data = ContextInfo.get_market_data_ex(
                    ["open", "high", "low", "close", "volume", "amount"],
                    batch, period="1d", end_time=api_end,
                    count=HISTORY_REQUEST_DAYS, dividend_type="front"
                )
            except Exception:
                data = ContextInfo.get_market_data_ex(
                    ["open", "high", "low", "close"],
                    batch, period="1d", end_time=api_end,
                    count=HISTORY_REQUEST_DAYS, dividend_type="front"
                )
            if data is None:
                raise RuntimeError("get_market_data_ex返回None")
            for stock in batch:
                try:
                    history = _history_from_frame(extract_stock_data(data, stock), requested)
                    if not history or not history.get("dates"):
                        failures[stock] = "没有有效日K"
                        continue
                    loaded[stock] = history
                except Exception as error:
                    failures[stock] = "%s: %s" % (type(error).__name__, error)
        except Exception as error:
            for stock in batch:
                failures[stock] = "批次异常 %s: %s" % (type(error).__name__, error)
        log("[历史预加载] 批次 %d/%d，成功 %d，失败 %d" % (
            batch_number, total_batches, len(loaded), len(failures)
        ), "INFO", requested)
    return loaded, failures


def _build_row(stock, trade_date, open_price, high_price, low_price, close_price,
               pre_close, volume=0.0, amount=0.0):
    open_price = _float(open_price)
    high_price = _float(high_price)
    low_price = _float(low_price)
    close_price = _float(close_price)
    pre_close = _float(pre_close)
    if min(open_price, high_price, low_price, close_price) <= 0:
        return None
    high_price = max(high_price, open_price, close_price)
    low_price = min(low_price, open_price, close_price)
    pct_chg = ((close_price - pre_close) / pre_close * 100.0) if pre_close > 0 else 0.0
    return {
        "ts_code": stock,
        "stock_code_with_market": stock,
        "trade_date": str(trade_date),
        "date_label": _date_label(trade_date),
        "open": round(open_price, 4),
        "high": round(high_price, 4),
        "low": round(low_price, 4),
        "close": round(close_price, 4),
        "pre_close": round(pre_close, 4),
        "pct_chg": round(pct_chg, 4),
        "vol": round(_float(volume), 4),
        "amount": round(_float(amount), 4),
    }


def _validated_history_rows(stock, history, ref_date):
    global g_history_rows_cache_date, g_history_rows_cache
    if not history:
        return None
    if g_history_rows_cache_date != ref_date:
        g_history_rows_cache_date = ref_date
        g_history_rows_cache = {}
    cached = g_history_rows_cache.get(stock)
    if cached is not None and cached[0] is history:
        return cached[1]
    fields = [history.get(key) or [] for key in ("dates", "opens", "highs", "lows", "closes")]
    total = min([len(value) for value in fields]) if fields else 0
    if total <= 0:
        return None
    volumes = history.get("volumes") or []
    amounts = history.get("amounts") or []

    rows_by_date = {}
    previous_close = 0.0
    for index in range(total):
        trade_date = _compact_date(fields[0][index])
        if not _is_valid_date(trade_date) or trade_date > ref_date:
            continue
        close_price = _float(fields[4][index])
        row = _build_row(
            stock, trade_date, fields[1][index], fields[2][index], fields[3][index],
            close_price, previous_close,
            volumes[index] if index < len(volumes) else 0.0,
            amounts[index] if index < len(amounts) else 0.0,
        )
        if row:
            rows_by_date[trade_date] = row
            previous_close = close_price

    g_history_rows_cache[stock] = (history, rows_by_date)
    return rows_by_date


def build_kline_payload(stock, name, history, tick, ref_date, updated_at=None):
    prepared = _validated_history_rows(stock, history, ref_date)
    if prepared is None:
        return None
    # Keep cached history immutable while always applying the current tick.
    rows_by_date = {day: dict(row) for day, row in prepared.items()}
    ordered_dates = sorted(rows_by_date)
    rows = [rows_by_date[value] for value in ordered_dates]

    if isinstance(tick, dict):
        tick_close = _float(tick.get("lastPrice") or tick.get("price"))
        existing = rows_by_date.get(ref_date)
        previous_row = None
        for row in rows:
            if row["trade_date"] < ref_date:
                previous_row = row
        tick_pre_close = _float(tick.get("lastClose") or tick.get("preClose"))
        if tick_pre_close <= 0 and previous_row:
            tick_pre_close = _float(previous_row.get("close"))
        if tick_close > 0 and tick_pre_close > 0:
            open_price = _float(tick.get("open")) or _float((existing or {}).get("open")) or tick_close
            high_price = _float(tick.get("high")) or _float((existing or {}).get("high")) or max(open_price, tick_close)
            low_price = _float(tick.get("low")) or _float((existing or {}).get("low")) or min(open_price, tick_close)
            if existing:
                high_price = max(high_price, _float(existing.get("high")))
                existing_low = _float(existing.get("low"))
                if existing_low > 0:
                    low_price = min(low_price, existing_low) if low_price > 0 else existing_low
            volume = _float(tick.get("volume") or tick.get("vol")) or _float((existing or {}).get("vol"))
            amount = _float(tick.get("amount")) or _float((existing or {}).get("amount"))
            live_row = _build_row(
                stock, ref_date, open_price, high_price, low_price, tick_close,
                tick_pre_close, volume, amount
            )
            if live_row:
                rows_by_date[ref_date] = live_row
                rows = [rows_by_date[value] for value in sorted(rows_by_date)]

    rows = rows[-REDIS_QMT_KLINE_DAYS:]
    if not rows:
        return None
    return {
        "code": _simple_code(stock),
        "ts_code": stock,
        "stock_code_with_market": stock,
        "name": str(name or _simple_code(stock)),
        "date": ref_date,
        "source": "qmt",
        "updated_at": updated_at or _now_text(),
        "rows": rows,
    }


def is_nearly_bare_foot(open_price, close_price, high_price, low_price):
    if close_price < open_price:
        return False
    bottom = min(open_price, close_price)
    lower_shadow = max(bottom - low_price, 0.0)
    return bool(
        lower_shadow <= 0
        or (bottom > 0 and lower_shadow / bottom * 100.0 < 0.1)
        or abs(low_price - open_price) <= 0.01
    )


def _row_ma5(rows, index):
    if index < 4:
        return 0.0
    closes = [_float(rows[pos].get("close")) for pos in range(index - 4, index + 1)]
    if len(closes) != 5 or min(closes) <= 0:
        return 0.0
    return sum(closes) / 5.0


def _row_previous_close(rows, index):
    previous = _float((rows[index] or {}).get("pre_close"))
    if previous <= 0 and index > 0:
        previous = _float((rows[index - 1] or {}).get("close"))
    return previous


def _row_max_gain(rows, index):
    previous = _row_previous_close(rows, index)
    high_price = _float((rows[index] or {}).get("high"))
    if previous <= 0 or high_price <= 0:
        return 0.0
    return (high_price - previous) / previous * 100.0


def _is_limit_down_close(close_price, previous_close, limit_ratio):
    if previous_close <= 0 or close_price <= 0:
        return False
    ratio = Decimal(str(limit_ratio)) / Decimal("100")
    price = Decimal(str(previous_close)) * (Decimal("1") - ratio)
    limit_price = float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return limit_price > 0 and close_price <= limit_price + 1e-8


def calculate_panzhong_flags(stock, rows, high_gain_10d_count):
    """Match PZ price-limit and five-pull flags without its selection gates."""
    flags = {
        "is_limit_up": 0,
        "ref_is_limit_up": False,
        "yi_yin_yi_yang": 0,
        "qiangchi": 0,
    }
    rows = rows or []
    if not rows:
        return flags
    ref = rows[-1] or {}
    code = _simple_code(
        stock or ref.get("stock_code_with_market") or ref.get("ts_code")
        or ref.get("code")
    )
    if code.startswith(("688", "300", "301")):
        limit_ratio = 20.0
    elif code.startswith(("8", "4")):
        limit_ratio = 30.0
    else:
        limit_ratio = 10.0
    previous_close = _row_previous_close(rows, len(rows) - 1)
    ref_close = _float(ref.get("close"))
    if previous_close > 0:
        limit_price = round(previous_close * (1.0 + limit_ratio / 100.0), 2)
        flags["is_limit_up"] = int(limit_price > 0 and ref_close >= limit_price - 0.01)
        flags["ref_is_limit_up"] = bool(flags["is_limit_up"])
    if len(rows) < 10 or int(high_gain_10d_count) < 5:
        return flags
    opens = [_float(row.get("open")) for row in rows[-10:]]
    closes = [_float(row.get("close")) for row in rows[-10:]]
    if min(opens + closes) <= 0:
        return flags
    if (
        closes[-1] < opens[-1]
        or _is_limit_down_close(closes[-1], previous_close, limit_ratio)
    ):
        return flags
    qiangchi = 1
    for index in range(9):
        if closes[index] >= opens[index]:
            continue
        next_close = closes[index + 1]
        if (
            next_close < opens[index + 1]
            or _is_limit_down_close(next_close, closes[index], limit_ratio)
        ):
            return flags
        if next_close < opens[index]:
            qiangchi = 0
    flags["yi_yin_yi_yang"] = 1
    flags["qiangchi"] = qiangchi
    return flags


def calculate_cleaning_metrics(rows, stock=""):
    rows = list(rows or [])
    if len(rows) < 24:
        return None

    end_index = len(rows) - 1
    start_20 = len(rows) - 20
    start_10 = len(rows) - 10
    yin_count_20 = sum(
        1 for index in range(start_20, end_index + 1)
        if _float(rows[index].get("close")) < _float(rows[index].get("open"))
    )

    high_gain_10d_count = 0
    low_above_ma5_count = 0
    yin_count_10d = 0
    yin_no_touch_count = 0
    yin_close_above_ma5_count = 0

    for index in range(start_10, end_index + 1):
        row = rows[index] or {}
        open_price = _float(row.get("open"))
        close_price = _float(row.get("close"))
        low_price = _float(row.get("low"))
        ma5 = _row_ma5(rows, index)
        if _row_max_gain(rows, index) >= 6.5:
            high_gain_10d_count += 1
        if ma5 > 0 and low_price > ma5:
            low_above_ma5_count += 1
        if close_price < open_price:
            yin_count_10d += 1
            if ma5 > 0 and low_price > ma5:
                yin_no_touch_count += 1
            if ma5 > 0 and close_price > ma5:
                yin_close_above_ma5_count += 1

    yin_pull_prev_or_next_fail = 0
    for index in range(start_10, end_index + 1):
        row = rows[index] or {}
        if _float(row.get("close")) >= _float(row.get("open")):
            continue
        previous_ok = index - 1 >= 1 and _row_max_gain(rows, index - 1) >= 6.5
        next_ok = index + 1 <= end_index and _row_max_gain(rows, index + 1) >= 6.5
        if not (previous_ok or next_ok):
            yin_pull_prev_or_next_fail += 1

    high_gain_10d_gte_5 = int(high_gain_10d_count >= 5)
    yin_pull_prev_or_next = int(yin_pull_prev_or_next_fail == 0)
    all_yin_no_touch = int(
        yin_count_10d == 0 or yin_no_touch_count == yin_count_10d
    )
    all_yin_close_above_ma5 = int(
        yin_count_10d == 0 or yin_close_above_ma5_count == yin_count_10d
    )

    ref = rows[end_index] or {}
    ref_open = _float(ref.get("open"))
    ref_high = _float(ref.get("high"))
    ref_low = _float(ref.get("low"))
    ref_close = _float(ref.get("close"))
    ref_pre_close = _row_previous_close(rows, end_index)
    ref_ma5 = _row_ma5(rows, end_index)
    ref_gain = (
        (ref_close - ref_pre_close) / ref_pre_close * 100.0
        if ref_pre_close > 0 else 0.0
    )
    max_gain = _row_max_gain(rows, end_index)
    ref_is_yang = int(ref_close >= ref_open)
    ref_high_gain_ok = bool(max_gain >= 6.5)
    ref_low_above_ma5 = int(ref_ma5 > 0 and ref_low > ref_ma5)
    bare_foot = int(is_nearly_bare_foot(ref_open, ref_close, ref_high, ref_low))
    bare_foot_yang = int(bare_foot == 1 and ref_high_gain_ok)
    bottom = min(ref_open, ref_close)
    lower_shadow = max(bottom - ref_low, 0.0)
    lower_shadow_pct = lower_shadow / bottom * 100.0 if bottom > 0 else 0.0

    result = {
        "yin_count_20": int(yin_count_20),
        "yin_count_10d": int(yin_count_10d),
        "yin_count_10": int(yin_count_10d),
        "is_quanyang_10d": int(yin_count_10d == 0),
        "high_gain_10d_count": int(high_gain_10d_count),
        "high_gain_10d_gte_5": high_gain_10d_gte_5,
        "low_above_ma5_count": int(low_above_ma5_count),
        "yin_no_touch_count": int(yin_no_touch_count),
        "all_yin_no_touch": all_yin_no_touch,
        "yin_close_above_ma5_count": int(yin_close_above_ma5_count),
        "all_yin_close_above_ma5": all_yin_close_above_ma5,
        "yin_total_10d": int(yin_count_10d),
        "yin_pull_prev_or_next_fail": int(yin_pull_prev_or_next_fail),
        "yin_pull_prev_or_next": yin_pull_prev_or_next,
        "high_gain5_and_yin_pull": int(
            high_gain_10d_gte_5 == 1 and yin_pull_prev_or_next == 1
        ),
        "ref_open": round(ref_open, 4),
        "ref_high": round(ref_high, 4),
        "ref_low": round(ref_low, 4),
        "ref_close": round(ref_close, 4),
        "ref_pre_close": round(ref_pre_close, 4),
        "ref_ma5": round(ref_ma5, 6),
        "ref_gain": round(ref_gain, 6),
        "max_gain": round(max_gain, 6),
        "is_ref_yang": ref_is_yang,
        "ref_high_gain_ok": ref_high_gain_ok,
        "ref_low_above_ma5": ref_low_above_ma5,
        "is_bare_foot": bare_foot,
        "is_bare_foot_yang": bare_foot_yang,
        "ref_lower_shadow_pct": round(lower_shadow_pct, 6),
        "purple_bare_foot_no_touch": int(
            bare_foot_yang == 1 and ref_low_above_ma5 == 1
        ),
        "red_lower_shadow_high_gain_no_touch": int(
            lower_shadow_pct <= 0.5 and max_gain > 6.5 and ref_low_above_ma5 == 1
        ),
    }
    result.update(calculate_panzhong_flags(stock, rows, high_gain_10d_count))
    return result


def classify_cleaning_stock(metrics, ths_metadata):
    metrics = dict(metrics or {})
    ths_metadata = dict(ths_metadata or {})
    change_percent = _optional_float(ths_metadata.get("ths_change_percent"))
    matched_by_gain = int(change_percent is not None and change_percent >= 4.5)

    purple_reasons = []
    if int(metrics.get("yin_count_20", 999)) <= 4:
        if int(bool(metrics.get("is_quanyang_10d"))):
            purple_reasons.append("quanyang_10d")
        if int(bool(metrics.get("all_yin_no_touch"))):
            purple_reasons.append("all_yin_no_touch")
        if int(bool(metrics.get("high_gain5_and_yin_pull"))):
            purple_reasons.append("high_gain5_and_yin_pull")
        if int(bool(metrics.get("purple_bare_foot_no_touch"))):
            purple_reasons.append("bare_foot_high_gain_low_above_ma5")

    purple_match = int(bool(purple_reasons))
    red_reasons = []
    yin_count_20 = int(metrics.get("yin_count_20", 999))
    if not purple_match and 4 < yin_count_20 <= 6:
        if int(bool(metrics.get("high_gain_10d_gte_5"))):
            red_reasons.append("high_gain_10d_gte_5")
        if int(bool(metrics.get("all_yin_close_above_ma5"))):
            red_reasons.append("all_yin_close_above_ma5")
        if int(metrics.get("low_above_ma5_count", 0)) >= 4:
            red_reasons.append("low_above_ma5_count_gte_4")
        if (
            _float(metrics.get("ref_lower_shadow_pct"), 999.0) <= 0.5
            and _float(metrics.get("max_gain")) > 6.5
            and int(bool(metrics.get("ref_low_above_ma5"))) == 1
        ):
            red_reasons.append("lower_shadow_high_gain_low_above_ma5")

    red_match = int(bool(red_reasons))
    selected = bool(matched_by_gain or purple_match or red_match)
    group_color = "purple" if purple_match else ("red" if red_match else "")
    group_name = "紫色组" if purple_match else ("红色组" if red_match else "")
    match_reasons = ["ths_change_percent_gte_4_5"] if matched_by_gain else []
    match_reasons.extend(purple_reasons if purple_match else red_reasons)
    return {
        "selected": selected,
        "matched_by_gain": matched_by_gain,
        "purple_match": purple_match,
        "red_match": red_match,
        "group_color": group_color,
        "group_name": group_name,
        "match_reasons": match_reasons,
        "purple_reasons": purple_reasons,
        "red_reasons": red_reasons,
    }


def build_cleaning_selection(stocks, names, metadata_by_stock, histories, ticks,
                             ref_date, updated_at):
    cleaned_stocks = {}
    selected_payloads = {}
    failed_stocks = []
    counts = {
        "candidate_count": len(stocks or []),
        "selected_count": 0,
        "purple_count": 0,
        "red_count": 0,
        "gain_only_count": 0,
        "unselected_count": 0,
        "metrics_incomplete_count": 0,
        "payload_failed_count": 0,
        "payload_build_seconds": 0.0,
        "metrics_classify_seconds": 0.0,
    }

    for stock in stocks or []:
        code = _simple_code(stock)
        build_started = time.time()
        payload = build_kline_payload(
            stock,
            (names or {}).get(stock),
            (histories or {}).get(stock),
            (ticks or {}).get(stock),
            ref_date,
            updated_at,
        )
        counts["payload_build_seconds"] += time.time() - build_started
        if not payload_covers_ref_date(payload, ref_date):
            failed_stocks.append(stock)
            counts["payload_failed_count"] += 1
            continue

        metadata = dict(
            (metadata_by_stock or {}).get(code)
            or (metadata_by_stock or {}).get(stock)
            or {}
        )
        classify_started = time.time()
        metrics = calculate_cleaning_metrics(payload.get("rows"), stock=stock)
        if metrics is None:
            counts["metrics_incomplete_count"] += 1
        classification = classify_cleaning_stock(metrics, metadata)
        counts["metrics_classify_seconds"] += time.time() - classify_started
        if not classification.get("selected"):
            counts["unselected_count"] += 1
            continue

        value = {
            "code": code,
            "name": str((names or {}).get(stock) or code),
            "stock_code_with_market": stock,
            "date": ref_date,
            "ref_date": ref_date,
            "updated_at": updated_at,
            "metrics_complete": int(metrics is not None),
            "yin_count_20": None,
            "yin_count_10d": None,
            "is_quanyang_10d": 0,
            "ths_change_percent": metadata.get("ths_change_percent"),
            "ths_price": _float(metadata.get("ths_price"), 0.0),
            "ths_snapshot_time": str(metadata.get("ths_snapshot_time") or ""),
            "board_names": list(metadata.get("board_names") or []),
            "limit_up_count": int(metadata.get("limit_up_count") or 0),
            "board_limit_up_counts": dict(metadata.get("board_limit_up_counts") or {}),
        }
        if metrics:
            value.update(metrics)
        else:
            value.update(calculate_panzhong_flags(stock, payload.get("rows"), 0))
        value.update(classification)
        cleaned_stocks[code] = value
        selected_payloads[code] = payload
        counts["selected_count"] += 1
        if classification.get("purple_match"):
            counts["purple_count"] += 1
        elif classification.get("red_match"):
            counts["red_count"] += 1
        else:
            counts["gain_only_count"] += 1

    return cleaned_stocks, selected_payloads, failed_stocks, counts


def payload_covers_ref_date(payload, ref_date):
    rows = (payload or {}).get("rows") or []
    if not rows:
        return False
    return _compact_date(rows[-1].get("trade_date")) == _compact_date(ref_date)


def should_accept_close_bar(bar, tick):
    close_price = _float((bar or {}).get("close"))
    open_price = _float((bar or {}).get("open"))
    high_price = _float((bar or {}).get("high"))
    low_price = _float((bar or {}).get("low"))
    if min(close_price, open_price, high_price, low_price) <= 0:
        return False
    last_close = _float((tick or {}).get("lastClose") or (tick or {}).get("preClose"))
    last_price = _float((tick or {}).get("lastPrice") or (tick or {}).get("price"))
    if (last_close > 0 and last_price > 0 and
            abs(close_price - last_close) < 0.001 and
            abs(last_price - last_close) > 0.001):
        return False
    return True


def _extract_last_bar(data, stock):
    frame = extract_stock_data(data, stock)
    if frame is None or len(frame) == 0:
        return None, ""
    index_value = frame.index[-1]
    try:
        trade_date = index_value.strftime("%Y%m%d")
    except Exception:
        trade_date = _compact_date(index_value)
    bar = {}
    for field in ("open", "high", "low", "close", "volume", "amount"):
        try:
            bar[field] = _float(frame[field].iloc[-1]) if field in frame else 0.0
        except Exception:
            bar[field] = 0.0
    return bar, trade_date


def overlay_close_after_market(ContextInfo, ticks, stocks, ref_date, now=None):
    global g_close_bar_cache_date, g_close_bar_cache, g_close_bar_last_fetch_at
    now = now or datetime.now()
    if now.strftime("%H%M%S") < "150000" or not ticks:
        return {"accepted": 0, "fake": 0, "missing": 0}

    if g_close_bar_cache_date != ref_date:
        g_close_bar_cache_date = ref_date
        g_close_bar_cache = {}
        g_close_bar_last_fetch_at = 0.0

    for stock, bar in g_close_bar_cache.items():
        tick = ticks.get(stock)
        if tick:
            tick.update({
                "lastPrice": bar["close"], "price": bar["close"],
                "open": bar["open"], "high": bar["high"], "low": bar["low"],
            })

    missing_stocks = [stock for stock in stocks if stock not in g_close_bar_cache]
    if not missing_stocks or time.time() - g_close_bar_last_fetch_at < CLOSE_BAR_REFRESH_SEC:
        return {"accepted": len(g_close_bar_cache), "fake": 0, "missing": len(missing_stocks)}

    g_close_bar_last_fetch_at = time.time()
    accepted = 0
    fake = 0
    missing = 0
    for offset in range(0, len(missing_stocks), HISTORY_BATCH_SIZE):
        batch = missing_stocks[offset:offset + HISTORY_BATCH_SIZE]
        try:
            data = ContextInfo.get_market_data_ex(
                ["open", "high", "low", "close", "volume", "amount"],
                batch, period="1d", end_time=ref_date, count=1, dividend_type="none"
            )
        except Exception as error:
            log("[收盘对齐] 日K请求失败: %s" % error, "WARN", ref_date)
            missing += len(batch)
            continue
        for stock in batch:
            bar, trade_date = _extract_last_bar(data, stock)
            tick = ticks.get(stock) or {}
            if not bar or trade_date != ref_date:
                missing += 1
                continue
            if not should_accept_close_bar(bar, tick):
                fake += 1
                continue
            g_close_bar_cache[stock] = bar
            tick.update({
                "lastPrice": bar["close"], "price": bar["close"],
                "open": bar["open"], "high": bar["high"], "low": bar["low"],
            })
            if bar.get("volume", 0) > 0:
                tick["volume"] = bar["volume"]
            if bar.get("amount", 0) > 0:
                tick["amount"] = bar["amount"]
            accepted += 1
    return {"accepted": accepted, "fake": fake, "missing": missing}


def _history_ticks(histories, ref_date):
    ticks = {}
    for stock, history in (histories or {}).items():
        dates = list(history.get("dates") or [])
        if ref_date not in dates:
            continue
        index = dates.index(ref_date)
        closes = list(history.get("closes") or [])
        if index >= len(closes):
            continue
        previous = _float(closes[index - 1]) if index > 0 else 0.0
        ticks[stock] = {
            "open": (history.get("opens") or [])[index],
            "high": (history.get("highs") or [])[index],
            "low": (history.get("lows") or [])[index],
            "lastPrice": closes[index], "price": closes[index],
            "lastClose": previous, "preClose": previous,
        }
    return ticks


# ==================== Redis发布与性能监控 ====================

def init_redis():
    global g_redis_client
    if redis is None:
        log("缺少redis模块，请先安装redis", "ERROR")
        return False
    try:
        g_redis_client = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
            db=REDIS_DB, decode_responses=True,
            socket_connect_timeout=10, socket_timeout=30,
            socket_keepalive=True, health_check_interval=30,
            retry_on_timeout=True,
        )
        g_redis_client.ping()
        log("Redis连接成功: %s:%s" % (REDIS_HOST, REDIS_PORT))
        return True
    except Exception as error:
        g_redis_client = None
        log("Redis连接失败: %s" % error, "ERROR")
        return False


def _status_key(date_text):
    return "qmt:kline:ths_publish_status:%s" % _compact_date(date_text)


def write_status(redis_client, date_text, mapping):
    if redis_client is None or not _compact_date(date_text):
        return
    values = {str(key): str(value) for key, value in mapping.items()}
    key = _status_key(date_text)
    try:
        pipe = redis_client.pipeline(transaction=False)
        if values:
            pipe.hset(key, mapping=values)
        pipe.expire(key, REDIS_STATUS_TTL_SEC)
        pipe.execute()
    except Exception as error:
        log("状态写入失败: %s" % error, "WARN", date_text)


def publish_payloads(redis_client, ref_date, payloads, ths_source_key, ths_stats,
                     started_at=None, target_count=None):
    if redis_client is None:
        raise RuntimeError("Redis未连接")
    ref_date = _compact_date(ref_date)
    if not _is_valid_date(ref_date):
        raise RuntimeError("无效交易日: %s" % ref_date)
    key = "%s:%s" % (REDIS_QMT_KLINE_KEY_PREFIX, ref_date)
    started_at = started_at or _now_text()
    payloads = dict(payloads or {})
    target_count = int(target_count if target_count is not None else len(payloads))
    mapping = {}
    total_bytes = 0
    for code, payload in payloads.items():
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        mapping[_simple_code(code)] = encoded
        total_bytes += len(encoded.encode("utf-8"))

    info = {
        "source": "qmt_ths",
        "source_key": ths_source_key,
        "date": ref_date,
        "days": REDIS_QMT_KLINE_DAYS,
        "target_count": target_count,
        "success_count": len(mapping),
        "failed_count": max(0, target_count - len(mapping)),
        "source_relations": int((ths_stats or {}).get("source_relations", 0)),
        "unique_codes": int((ths_stats or {}).get("unique_codes", 0)),
        "excluded_risk": int((ths_stats or {}).get("excluded_risk", 0)),
        "started_at": started_at,
        "updated_at": _now_text(),
        "ttl_sec": REDIS_QMT_KLINE_TTL_SEC,
    }
    info_encoded = json.dumps(info, ensure_ascii=False, separators=(",", ":"))
    total_bytes += len(info_encoded.encode("utf-8"))

    pipe = redis_client.pipeline(transaction=False)
    mapping["_ths_snapshot_info"] = info_encoded
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, REDIS_QMT_KLINE_TTL_SEC)
    pipe.execute()
    return {
        "redis_key": key,
        "success_count": len(mapping) - 1,
        "failed_count": max(0, target_count - (len(mapping) - 1)),
        "total_bytes": total_bytes,
    }


def publish_cleaning_results(redis_client, ref_date, stocks, scan_info, cycle=0):
    if redis_client is None:
        raise RuntimeError("Redis未连接")
    ref_date = _compact_date(ref_date)
    if not _is_valid_date(ref_date):
        raise RuntimeError("无效交易日: %s" % ref_date)
    target_key = "%s:%s" % (THS_CLEANING_KEY_PREFIX, ref_date)
    temp_key = "%s:tmp:%s" % (target_key, uuid.uuid4().hex)
    mapping = {
        str(_simple_code(code)): json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        )
        for code, value in (stocks or {}).items()
        if _simple_code(code)
    }
    mapping["_scan_info"] = json.dumps(
        scan_info or {}, ensure_ascii=False, separators=(",", ":")
    )

    pipe = redis_client.pipeline(transaction=True)
    pipe.hset(temp_key, mapping=mapping)
    pipe.expire(temp_key, REDIS_QMT_KLINE_TTL_SEC)
    pipe.rename(temp_key, target_key)
    pipe.execute()
    return {
        "redis_key": target_key,
        "success_count": len(mapping) - 1,
    }


def evaluate_performance(metrics):
    warnings = []
    if _float(metrics.get("tick_seconds")) > 1.0:
        warnings.append("QMT快照超过1秒")
    if _float(metrics.get("redis_seconds")) > 5.0:
        warnings.append("Redis写入超过5秒")
    total_seconds = _float(metrics.get("total_seconds"))
    if total_seconds > 1.0:
        warnings.append("总耗时超过1秒")
        warnings.append("1秒频率无法稳定完成")
    target = int(metrics.get("target_count") or 0)
    success = int(metrics.get("success_count") or 0)
    if target > 0 and success / float(target) < 0.95:
        warnings.append("成功率低于95%")
    return warnings


def _p95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, int(math.ceil(len(ordered) * 0.95)) - 1)
    return ordered[index]


def summarize_recent_performance():
    values = [item.get("total_seconds", 0.0) for item in g_recent_metrics]
    if not values:
        return {"mean": 0.0, "max": 0.0, "p95": 0.0, "success_rate": 0.0}
    successes = sum(item.get("success_count", 0) for item in g_recent_metrics)
    targets = sum(item.get("target_count", 0) for item in g_recent_metrics)
    return {
        "mean": sum(values) / len(values),
        "max": max(values),
        "p95": _p95(values),
        "success_rate": successes / float(targets) if targets else 0.0,
    }


PERFORMANCE_COLUMNS = [
    "time", "ths_source_key", "ref_date", "source_relations", "unique_codes",
    "candidate_count", "purple_count", "red_count", "gain_only_count",
    "selected_count", "target_count", "success_count", "failed_count", "excluded_risk",
    "ths_seconds", "history_seconds", "tick_seconds", "close_overlay_seconds",
    "build_seconds", "cleaning_seconds", "cleaning_redis_seconds",
    "redis_seconds", "total_seconds", "total_bytes",
    "throughput_mb_s", "busy_skip_count", "warnings",
]


def write_performance_csv(metrics, date_text):
    path = os.path.join(get_log_dir(date_text), "性能监控.csv")
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=PERFORMANCE_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        row = dict(metrics)
        row["warnings"] = "；".join(metrics.get("warnings") or [])
        writer.writerow(row)


def run_with_cycle_lock(lock, callback):
    if not lock.acquire(blocking=False):
        return "busy_skip"
    try:
        return callback()
    finally:
        lock.release()


# ==================== 每轮同步 ====================

def build_market_code_map(ContextInfo):
    result = {}
    sector_names = ["沪深A股", "科创板", "北交所A股"]
    for sector in sector_names:
        try:
            stocks = ContextInfo.get_stock_list_in_sector(sector) or []
            log("市场代码表 %s: %d只" % (sector, len(stocks)))
        except Exception as error:
            log("市场代码表 %s 获取失败: %s" % (sector, error), "WARN")
            continue
        for stock in stocks:
            code = _simple_code(stock)
            if code:
                result[code] = str(stock).upper()
    return result


def _load_qmt_names(ContextInfo, payload):
    source_codes = sorted({_simple_code((item or {}).get("code"))
                           for item in _ths_stock_relations(payload)})
    for code in source_codes:
        if not code:
            continue
        stock = g_market_code_map.get(code) or _infer_market_stock(code)
        if not stock or stock in g_stock_names:
            continue
        try:
            name = ContextInfo.get_stock_name(stock)
            if name:
                g_stock_names[stock] = str(name)
        except Exception:
            pass


def refresh_ths_universe(ContextInfo, force_reload=False):
    global g_stock_pool, g_stock_names, g_history_data, g_ths_source_key, g_ths_stats
    global g_ths_stock_metadata
    global g_history_rows_cache
    ths_started = time.time()
    source_key, payload = resolve_ths_source(g_redis_client, REFERENCE_DATE)
    all_metadata = collect_ths_stock_metadata(payload)
    _load_qmt_names(ContextInfo, payload)
    stocks, names, stats = select_ths_universe(payload, g_market_code_map, g_stock_names)
    if not stocks:
        raise RuntimeError("THS快照没有可用股票")
    g_stock_names.update(names)
    selected_codes = {_simple_code(stock) for stock in stocks}
    g_ths_stock_metadata = {
        code: value for code, value in all_metadata.items()
        if code in selected_codes
    }
    stats["ths_seconds"] = time.time() - ths_started

    old_history = dict(g_history_data)
    if force_reload:
        requested = stocks
    else:
        requested = [stock for stock in stocks if stock not in g_history_data]
    loaded = {}
    failures = {}
    history_started = time.time()
    if requested:
        loaded, failures = load_history_for_stocks(ContextInfo, requested, REFERENCE_DATE)
        if force_reload:
            new_history = dict(loaded)
            for stock in requested:
                if stock not in new_history and stock in old_history:
                    new_history[stock] = old_history[stock]
            g_history_data = new_history
        else:
            g_history_data.update(loaded)

    g_stock_pool = stocks
    if force_reload:
        g_history_rows_cache = {}
    else:
        active_stocks = set(stocks)
        g_history_rows_cache = {stock: value for stock, value in g_history_rows_cache.items()
                                if stock in active_stocks}
    g_ths_source_key = source_key
    stats["history_requested"] = len(requested)
    stats["history_loaded"] = len(loaded)
    stats["history_failed"] = len(failures)
    stats["history_seconds"] = time.time() - history_started if requested else 0.0
    g_ths_stats = stats
    if failures:
        samples = list(failures.items())[:10]
        for stock, reason in samples:
            log("[历史预加载失败] %s %s" % (stock, reason), "WARN", REFERENCE_DATE)
        if len(failures) > len(samples):
            log("[历史预加载失败] 另有%d只未逐条显示" % (len(failures) - len(samples)), "WARN", REFERENCE_DATE)
    return source_key, stocks, stats


def _record_cycle_metrics(metrics):
    global g_consecutive_slow_cycles
    metrics["warnings"] = evaluate_performance(metrics)
    g_recent_metrics.append(dict(metrics))
    if metrics.get("total_seconds", 0.0) > 1.0:
        g_consecutive_slow_cycles += 1
    else:
        g_consecutive_slow_cycles = 0
    if g_consecutive_slow_cycles >= 3:
        detail = "连续3轮超过1秒，1秒频率无法稳定完成"
        if detail not in metrics["warnings"]:
            metrics["warnings"].append(detail)
    try:
        write_performance_csv(metrics, metrics.get("ref_date"))
    except Exception as error:
        log("[性能] CSV写入失败，但不影响Redis发布: %s" % error,
            "WARN", metrics.get("ref_date"))
    recent = summarize_recent_performance()
    log("[性能] 本轮 %.3fs | THS %.3fs | 历史 %.3fs | 快照 %.3fs | 构建 %.3fs | 清洗 %.3fs | Redis %.3fs(清洗Key %.3fs) | 最近%d轮均值 %.3fs/P95 %.3fs/最大 %.3fs/成功率 %.1f%% | 忙跳过%d次" % (
        metrics.get("total_seconds", 0.0), metrics.get("ths_seconds", 0.0),
        metrics.get("history_seconds", 0.0), metrics.get("tick_seconds", 0.0),
        metrics.get("build_seconds", 0.0), metrics.get("cleaning_seconds", 0.0),
        metrics.get("redis_seconds", 0.0), metrics.get("cleaning_redis_seconds", 0.0),
        len(g_recent_metrics), recent["mean"], recent["p95"], recent["max"],
        recent["success_rate"] * 100.0, g_busy_skip_count
    ), "INFO", metrics.get("ref_date"))
    for warning in metrics["warnings"]:
        level = "ERROR" if "无法稳定" in warning else "WARN"
        log("[性能警告] %s" % warning, level, metrics.get("ref_date"))


def _publish_cycle_impl(ContextInfo, force_reload=False, allow_latest_trade_date=False):
    global g_cycle_count, g_last_reload_date, g_last_cleaning_signature
    cycle_started = time.time()
    started_at = _now_text()
    now = datetime.now()
    status_date = _compact_date(REFERENCE_DATE) or now.strftime("%Y%m%d")
    g_cycle_count += 1
    stage = "start"
    metrics = {
        "time": started_at,
        "ths_source_key": "",
        "ref_date": status_date,
        "source_relations": 0,
        "unique_codes": 0,
        "candidate_count": 0,
        "purple_count": 0,
        "red_count": 0,
        "gain_only_count": 0,
        "selected_count": 0,
        "target_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "excluded_risk": 0,
        "ths_seconds": 0.0,
        "history_seconds": 0.0,
        "tick_seconds": 0.0,
        "close_overlay_seconds": 0.0,
        "build_seconds": 0.0,
        "cleaning_seconds": 0.0,
        "cleaning_redis_seconds": 0.0,
        "redis_seconds": 0.0,
        "total_seconds": 0.0,
        "total_bytes": 0,
        "throughput_mb_s": 0.0,
        "busy_skip_count": g_busy_skip_count,
    }
    write_status(g_redis_client, status_date, {
        "state": "running", "start_time": started_at,
        "success_time": "", "failure_time": "", "failure_stage": "", "error": "",
        "cycle": g_cycle_count,
    })

    try:
        stage = "ths_read"
        reload_now = force_reload or (not REFERENCE_DATE and should_reload_history(now, g_last_reload_date))
        source_key, stocks, stats = refresh_ths_universe(ContextInfo, reload_now)
        metrics["history_seconds"] = stats.get("history_seconds", 0.0)
        metrics["ths_seconds"] = stats.get("ths_seconds", 0.0)
        metrics["ths_source_key"] = source_key
        metrics["source_relations"] = stats.get("source_relations", 0)
        metrics["unique_codes"] = stats.get("unique_codes", 0)
        metrics["excluded_risk"] = stats.get("excluded_risk", 0)
        metrics["target_count"] = len(stocks)
        if reload_now and not REFERENCE_DATE:
            g_last_reload_date = now.strftime("%Y%m%d")
            log("[09:25历史重载] 完成：目标%d只，成功%d只，失败%d只" % (
                len(stocks), len([s for s in stocks if s in g_history_data]),
                len([s for s in stocks if s not in g_history_data])
            ), "INFO", status_date)

        stage = "qmt_snapshot"
        tick_start = time.time()
        if REFERENCE_DATE:
            provisional_date = resolve_qmt_trade_date(g_history_data, {}, now, REFERENCE_DATE)
            ticks = _history_ticks(g_history_data, provisional_date)
        else:
            provisional_date = resolve_qmt_trade_date(g_history_data, {}, now)
            if should_request_realtime_ticks(now, provisional_date, allow_latest_trade_date):
                ticks = ContextInfo.get_full_tick(stocks) or {}
            else:
                ticks = {}
                log("[盘外单次] 使用最近交易日%s的历史K，不合并实时快照" % provisional_date,
                    "INFO", provisional_date)
        metrics["tick_seconds"] = time.time() - tick_start

        ref_date = resolve_qmt_trade_date(g_history_data, ticks, now, REFERENCE_DATE)
        if not ref_date:
            raise RuntimeError("QMT历史数据中没有可确认的实际交易日")
        metrics["ref_date"] = ref_date
        status_date = ref_date
        if (not REFERENCE_DATE and not allow_latest_trade_date and
                not is_confirmed_current_trade_day(now, ref_date)):
            metrics["total_seconds"] = time.time() - cycle_started
            write_status(g_redis_client, now.strftime("%Y%m%d"), {
                "state": "skipped", "start_time": started_at,
                "success_time": "", "failure_time": "",
                "failure_stage": "trade_date_check", "error": "",
                "reason": "QMT未确认今天为交易日",
                "resolved_trade_date": ref_date,
                "duration_seconds": "%.3f" % metrics["total_seconds"],
            })
            log("[跳过发布] QMT最新有效交易日为%s，尚未确认今天%s为交易日" % (
                ref_date, now.strftime("%Y%m%d")
            ), "WARN", now.strftime("%Y%m%d"))
            return {"skipped": True, "reason": "unconfirmed_trade_day", "ref_date": ref_date}

        stage = "close_overlay"
        overlay_start = time.time()
        close_info = {"accepted": 0, "fake": 0, "missing": 0}
        if not REFERENCE_DATE:
            close_info = overlay_close_after_market(ContextInfo, ticks, stocks, ref_date, now)
        metrics["close_overlay_seconds"] = time.time() - overlay_start
        if now.strftime("%H%M%S") >= "150000" and not REFERENCE_DATE:
            log("[收盘对齐] 正式日K%d只，占位日K%d只，缺失%d只" % (
                close_info["accepted"], close_info["fake"], close_info["missing"]
            ), "INFO", ref_date)

        stage = "cleaning"
        cleaning_start = time.time()
        updated_at = _now_text()
        cleaned_stocks, payloads, failed_stocks, cleaning_counts = build_cleaning_selection(
            stocks, g_stock_names, g_ths_stock_metadata, g_history_data,
            ticks, ref_date, updated_at
        )
        metrics["build_seconds"] = cleaning_counts.pop("payload_build_seconds", 0.0)
        metrics["cleaning_seconds"] = time.time() - cleaning_start
        metrics.update(cleaning_counts)
        metrics["candidate_count"] = len(stocks)
        metrics["target_count"] = len(payloads)

        log("[清洗] THS候选%d只 | 紫色%d只 | 红色%d只 | 仅涨幅%d只 | 最终%d只 | K线缺失%d只" % (
            len(stocks), metrics.get("purple_count", 0), metrics.get("red_count", 0),
            metrics.get("gain_only_count", 0), metrics.get("selected_count", 0),
            metrics.get("payload_failed_count", 0)
        ), "INFO", ref_date)

        signature = json.dumps([
            (code, value.get("group_color"), value.get("match_reasons"))
            for code, value in sorted(cleaned_stocks.items())
        ], ensure_ascii=False, separators=(",", ":"))
        if signature != g_last_cleaning_signature:
            for code, value in sorted(cleaned_stocks.items()):
                log("[清洗入选] %s %s | 颜色:%s | THS涨幅:%.2f%% | 20日阴线:%d | 原因:%s" % (
                    code, value.get("name", ""), value.get("group_color") or "无",
                    _float(value.get("ths_change_percent")),
                    int(value.get("yin_count_20", 0)),
                    ",".join(value.get("match_reasons") or [])
                ), "INFO", ref_date)
            g_last_cleaning_signature = signature

        stage = "kline_redis_publish"
        redis_start = time.time()
        kline_publish_error = ""
        try:
            publish_result = publish_payloads(
                g_redis_client, ref_date, payloads, source_key, stats,
                started_at=started_at, target_count=len(payloads)
            )
        except Exception as publish_error:
            kline_publish_error = "%s: %s" % (
                type(publish_error).__name__, publish_error
            )
            publish_result = {
                "redis_key": "%s:%s" % (REDIS_QMT_KLINE_KEY_PREFIX, ref_date),
                "success_count": 0,
                "failed_count": len(payloads),
                "total_bytes": 0,
            }
            log("[K线发布失败] 清洗结果仍继续写入: %s" % kline_publish_error,
                "ERROR", ref_date)

        stage = "cleaning_redis_publish"
        cleaning_redis_started = time.time()
        scan_info = {
            "source": "qmt_ths_cleaning",
            "source_key": source_key,
            "date": ref_date,
            "candidate_count": len(stocks),
            "selected_count": metrics.get("selected_count", 0),
            "purple_count": metrics.get("purple_count", 0),
            "red_count": metrics.get("red_count", 0),
            "gain_only_count": metrics.get("gain_only_count", 0),
            "unselected_count": metrics.get("unselected_count", 0),
            "payload_failed_count": metrics.get("payload_failed_count", 0),
            "metrics_incomplete_count": metrics.get("metrics_incomplete_count", 0),
            "kline_success_count": publish_result.get("success_count", 0),
            "kline_failed_count": publish_result.get("failed_count", 0),
            "kline_publish_error": kline_publish_error,
            "started_at": started_at,
            "updated_at": _now_text(),
            "cycle": g_cycle_count,
            "ttl_sec": REDIS_QMT_KLINE_TTL_SEC,
        }
        cleaning_result = publish_cleaning_results(
            g_redis_client, ref_date, cleaned_stocks, scan_info, g_cycle_count
        )
        metrics["cleaning_redis_seconds"] = time.time() - cleaning_redis_started
        metrics["redis_seconds"] = time.time() - redis_start
        metrics["success_count"] = publish_result["success_count"]
        metrics["failed_count"] = publish_result["failed_count"]
        metrics["total_bytes"] = publish_result["total_bytes"]
        if metrics["redis_seconds"] > 0:
            metrics["throughput_mb_s"] = (
                metrics["total_bytes"] / 1024.0 / 1024.0 / metrics["redis_seconds"]
            )
        metrics["total_seconds"] = time.time() - cycle_started
        _record_cycle_metrics(metrics)

        write_status(g_redis_client, ref_date, {
            "state": "partial_success" if kline_publish_error else "success",
            "start_time": started_at,
            "success_time": _now_text(), "failure_time": "",
            "failure_stage": "", "error": "",
            "source_key": source_key, "redis_key": publish_result["redis_key"],
            "cleaning_redis_key": cleaning_result["redis_key"],
            "candidate_count": len(stocks),
            "selected_count": metrics.get("selected_count", 0),
            "purple_count": metrics.get("purple_count", 0),
            "red_count": metrics.get("red_count", 0),
            "gain_only_count": metrics.get("gain_only_count", 0),
            "target_count": len(payloads), "success_count": publish_result["success_count"],
            "failed_count": publish_result["failed_count"],
            "kline_publish_error": kline_publish_error,
            "cleaning_seconds": "%.3f" % metrics.get("cleaning_seconds", 0.0),
            "duration_seconds": "%.3f" % metrics["total_seconds"],
            "busy_skip_count": g_busy_skip_count,
        })
        log("[第%d轮] 清洗与K线发布%s：候选%d只，入选%d只，K线成功%d只，失败%d只，%.3fs" % (
            g_cycle_count, "部分成功" if kline_publish_error else "成功", len(stocks),
            metrics.get("selected_count", 0),
            publish_result["success_count"], publish_result["failed_count"],
            metrics["total_seconds"]
        ), "INFO", ref_date)
        if failed_stocks:
            log("[构建失败] %s" % ",".join(failed_stocks[:20]), "WARN", ref_date)
        publish_result["cleaning_redis_key"] = cleaning_result["redis_key"]
        publish_result["selected_count"] = metrics.get("selected_count", 0)
        return publish_result
    except Exception as error:
        metrics["total_seconds"] = time.time() - cycle_started
        metrics["failed_count"] = metrics.get("target_count", 0)
        try:
            _record_cycle_metrics(metrics)
        except Exception:
            pass
        write_status(g_redis_client, status_date, {
            "state": "failed", "start_time": started_at,
            "success_time": "", "failure_time": _now_text(),
            "failure_stage": stage,
            "error": "%s: %s" % (type(error).__name__, error),
            "duration_seconds": "%.3f" % metrics["total_seconds"],
            "busy_skip_count": g_busy_skip_count,
        })
        log("[第%d轮] 发布失败，阶段=%s，错误=%s: %s\n%s" % (
            g_cycle_count, stage, type(error).__name__, error, traceback.format_exc()
        ), "ERROR", status_date)
        return None


def _record_busy_skip(now=None):
    global g_busy_skip_count
    g_busy_skip_count += 1
    now = now or datetime.now()
    date_text = _compact_date(REFERENCE_DATE) or now.strftime("%Y%m%d")
    write_status(g_redis_client, date_text, {
        "busy_skip_count": g_busy_skip_count,
        "last_busy_skip_time": _now_text(now),
    })
    if g_busy_skip_count == 1 or g_busy_skip_count % 10 == 0:
        log("上一轮仍未结束，本轮跳过；累计跳过%d次" % g_busy_skip_count, "WARN", date_text)


def publish_ths_kline(ContextInfo):
    if REFERENCE_DATE:
        return
    now = datetime.now()
    if not is_trading_session(now):
        return
    result = run_with_cycle_lock(g_cycle_lock, lambda: _publish_cycle_impl(ContextInfo))
    if result == "busy_skip":
        _record_busy_skip(now)


def reload_history_data(ContextInfo):
    if REFERENCE_DATE:
        return
    now = datetime.now()
    if now.weekday() >= 5:
        return
    result = run_with_cycle_lock(
        g_cycle_lock, lambda: _publish_cycle_impl(ContextInfo, force_reload=True)
    )
    if result == "busy_skip":
        _record_busy_skip(now)


def handlebar(ContextInfo):
    pass


def init(ContextInfo):
    global g_context, g_initialized, g_market_code_map
    if g_initialized:
        return
    g_initialized = True
    g_context = ContextInfo

    log("=" * 70)
    log("THS股票清洗与K线一秒连续同步程序初始化")
    log("[CONFIG] optimization=ths_latest_fastpath_history_cache_pipeline_v1")
    log("=" * 70)
    if not init_redis():
        return

    try:
        ContextInfo.subscribe_whole_quote(["SH", "SZ"])
        log("全市场行情订阅成功: SH、SZ")
    except Exception as error:
        log("全市场行情订阅失败，将继续尝试按股票取快照: %s" % error, "WARN")

    g_market_code_map = build_market_code_map(ContextInfo)
    log("市场代码映射完成: %d只" % len(g_market_code_map))

    if REFERENCE_DATE:
        log("历史模式: %s，只执行一次" % REFERENCE_DATE)
        run_with_cycle_lock(
            g_cycle_lock, lambda: _publish_cycle_impl(ContextInfo, force_reload=True)
        )
        return

    ContextInfo.run_time(
        "reload_history_data", "1nDay", "2024-01-01 09:25:00", "SH"
    )
    ContextInfo.run_time(
        "publish_ths_kline", PUBLISH_INTERVAL_TEXT, "2024-01-01 09:25:00", "SH"
    )
    log("定时器已设置: 每天09:25重载历史，交易时段每秒触发同步")

    now = datetime.now()
    if is_trading_session(now):
        log("当前处于交易时段，立即执行首次历史加载和发布")
        reload_history_data(ContextInfo)
    else:
        log("当前不在交易时段，按最近实际交易日执行一次更新")
        run_with_cycle_lock(
            g_cycle_lock,
            lambda: _publish_cycle_impl(
                ContextInfo, force_reload=True, allow_latest_trade_date=True
            )
        )
