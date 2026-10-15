"""Local Redis review formatting; no Feishu credentials or publishing jobs."""

from __future__ import annotations

import os

import base64

from datetime import date, datetime

import json

import re

from typing import Any

import unicodedata

COLS = 8

PRIMARY_END_COLUMN = "H"

FOCUS_PAGE_URL = os.environ.get("FOCUS_PAGE_URL", "/重点关注")

FOCUS_LINK_TEXT = "筛选出跌破5日线少的并且漂亮K多的（点击跳转）"

YIN4_LINK_TEXT = "筛选有一字或者厂子形态的股（点击跳转）"

YIN4_SECTION_TITLE = "阴小4（必做任务）"

YIN_BELOW_SECTION_TITLE = "阴线跌破5日线（必做任务）"

YIN_BELOW_GUIDE = "筛选一字或者厂子"

YIN4_NOTES = (
    "1.一字横盘",
    "2.厂子横盘",
    "3.全阳都在5日上（几乎都不下插5日线 通达创智）",
)

YIN_MA5_CRITERIA = "20日阴线<=6，阴线收盘低于MA5次数<=1"

FIVE_PRIORITY_FIELDS = (
    ("yi_yin_yi_yang", "1.5拉一阴一阳"),
    ("qiangchi", "2.5拉强吃"),
)

FIVE_PRIORITY_GROUP_PREFIX = "10天阴线="

LIMIT_UP_TAG_COLOR = "#9333EA"

BARE_FOOT_TAG_COLOR = "#B45309"

STOCK_TAG_RIGHT_UNITS = (54, 46, 54, 46, 54, 46, 54, 46)

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default

def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default

def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

def decode_payload(raw: Any):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        try:
            return json.loads(base64.b64decode(text).decode("utf-8"))
        except Exception:
            return None

def redis_client():
    import redis
    import redis_config

    return redis.Redis(
        host=redis_config.REDIS_HOST,
        port=redis_config.REDIS_PORT,
        password=redis_config.REDIS_PASSWORD,
        db=redis_config.REDIS_DB,
        decode_responses=False,
        socket_connect_timeout=10,
        socket_timeout=20,
    )

def load_hash_stocks(client, key: str):
    stocks = []
    for field, raw_value in (client.hgetall(key) or {}).items():
        if isinstance(field, bytes):
            field = field.decode("utf-8", errors="ignore")
        if str(field).startswith("_"):
            continue
        stock = decode_payload(raw_value)
        if isinstance(stock, dict):
            stocks.append(stock)
    return stocks

def calculate_qy2_from_kline_rows(rows: list[dict[str, Any]]) -> bool:
    valid_rows = [row for row in rows if isinstance(row, dict)]
    valid_rows.sort(key=lambda row: str(row.get("trade_date") or ""))
    if len(valid_rows) < 14:
        return False
    start = len(valid_rows) - 10
    for index in range(start, len(valid_rows)):
        current = valid_rows[index]
        open_value = safe_float(current.get("open"))
        close_value = safe_float(current.get("close"))
        if close_value >= open_value:
            continue
        ma5 = sum(
            safe_float(valid_rows[pos].get("close"))
            for pos in range(index - 4, index + 1)
        ) / 5.0
        low_above_ma5 = ma5 > 0 and safe_float(current.get("low")) + 1e-9 >= ma5
        next_eaten_by_yang = False
        if index + 1 < len(valid_rows):
            following = valid_rows[index + 1]
            next_eaten_by_yang = (
                safe_float(following.get("close")) > safe_float(following.get("open"))
                and safe_float(following.get("close")) > open_value
            )
        if not (low_above_ma5 or next_eaten_by_yang):
            return False
    return True

def synthesize_qy2_from_pz_kline(client, run_date: date):
    compact = run_date.strftime("%Y%m%d")
    history_payloads = {
        normalize_stock_code(field): decode_payload(raw_value)
        for field, raw_value in (client.hgetall("pz:kline:history:%s" % compact) or {}).items()
        if not str(field.decode("utf-8", errors="ignore") if isinstance(field, bytes) else field).startswith("_")
    }
    realtime_payloads = {
        normalize_stock_code(field): decode_payload(raw_value)
        for field, raw_value in (client.hgetall("pz:kline:realtime:%s" % compact) or {}).items()
        if not str(field.decode("utf-8", errors="ignore") if isinstance(field, bytes) else field).startswith("_")
    }
    selected = []
    for code, realtime in realtime_payloads.items():
        if not code or not isinstance(realtime, dict):
            continue
        history = history_payloads.get(code)
        rows = list((history or {}).get("rows") or []) if isinstance(history, dict) else []
        realtime_row = realtime.get("row")
        if isinstance(realtime_row, dict):
            ref_date = str(realtime_row.get("trade_date") or compact)
            rows = [row for row in rows if str(row.get("trade_date") or "") != ref_date]
            rows.append(realtime_row)
        if calculate_qy2_from_kline_rows(rows):
            selected.append({
                "code": code,
                "name": str(realtime.get("name") or (history or {}).get("name") or code),
                "qy2_all_yin_low_gte_ma5_or_eaten_10d": 1,
                "qy2_source": "pz_kline_fallback",
            })
    return selected

def load_daily_qy2(run_date: date):
    client = redis_client()
    qy2_key = "qy2:%s" % run_date.strftime("%Y%m%d")
    stocks = load_hash_stocks(client, qy2_key)
    if stocks:
        return qy2_key, stocks, "qy2"
    return qy2_key, synthesize_qy2_from_pz_kline(client, run_date), "pz_kline_fallback"

def display_units(value: Any) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1
        for char in str(value or "")
    )

def hyperlink_value(url: str, label: str) -> list[dict[str, Any]]:
    return [{"type": "url", "text": str(label or ""), "link": str(url or "")}]

def hyperlink_display_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item or "")
            for item in value
        )
    text = str(value or "")
    match = re.fullmatch(
        r'=HYPERLINK\("(?:[^"]|"")*","((?:[^"]|"")*)"\)',
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).replace('""', '"') if match else text

def calculate_column_widths(values: list[list[Any]], cols: int = COLS) -> list[int]:
    widths = []
    for column in range(cols):
        maximum = max(
            (
                display_units(hyperlink_display_text(row[column]))
                if column < len(row) else 0
                for row in values
            ),
            default=0,
        )
        widths.append(max(184, min(1000, maximum * 8 + 48)))
    return widths

def normalize_stock_name(value: Any) -> str:
    return "".join(str(value or "").split())

def pad_stock_name(name: str, positions: int = 5) -> str:
    target_units = positions * 2
    current_units = display_units(name)
    if current_units >= target_units:
        return name + "  "
    missing_units = target_units - current_units
    return name + ("　" * (missing_units // 2)) + (" " * (missing_units % 2))

def stock_gain(stock: dict[str, Any]) -> float:
    return safe_float(stock.get("today_change", stock.get("ref_gain", 0.0)))

def format_stock(stock: dict[str, Any]) -> str:
    name = normalize_stock_name(stock.get("name") or stock.get("code") or "")
    return "%s%+.2f%%" % (pad_stock_name(name), stock_gain(stock))

def stock_tag_segments(
    prefix: str, column: int, is_limit_up: bool, is_bare_foot: bool,
) -> list[dict[str, Any]]:
    if not is_limit_up and not is_bare_foot:
        return []
    gap = "\u3000\u3000"
    reserved_units = (
        display_units("涨停" + gap + "几乎光脚")
        if is_limit_up else display_units("几乎光脚")
    )
    padding_units = max(
        2,
        STOCK_TAG_RIGHT_UNITS[column] - display_units(prefix) - reserved_units,
    )
    padding = "\u3000" * (padding_units // 2) + " " * (padding_units % 2)
    segments = []
    if is_limit_up:
        segments.append({"type": "text", "text": padding + "涨停",
                         "segmentStyle": {"bold": True, "foreColor": LIMIT_UP_TAG_COLOR}})
    if is_bare_foot:
        segments.append({"type": "text", "text": (gap if is_limit_up else padding) + "几乎光脚",
                         "segmentStyle": {"bold": True, "foreColor": BARE_FOOT_TAG_COLOR}})
    return segments

def format_stock_cell(stock: dict[str, Any], column: int = 0) -> str | list[dict[str, Any]]:
    is_limit_up = safe_int(stock.get("is_limit_up")) == 1
    is_bare_foot = safe_int(stock.get("is_bare_foot")) == 1
    if not is_limit_up and not is_bare_foot:
        return format_stock(stock)
    gain = stock_gain(stock)
    name = normalize_stock_name(stock.get("name") or stock.get("code") or "")
    gain_color = "#D93025" if gain > 0 else "#188038" if gain < 0 else "#000000"
    name_text = pad_stock_name(name)
    gain_text = "%+.2f%%" % gain
    return [
        {"type": "text", "text": name_text,
         "segmentStyle": {"foreColor": "#000000"}},
        {"type": "text", "text": gain_text,
         "segmentStyle": {"foreColor": gain_color}},
        *stock_tag_segments(name_text + gain_text, column, is_limit_up, is_bare_foot),
    ]

def stock_gain_direction(stock: dict[str, Any]) -> str:
    gain = stock_gain(stock)
    if gain > 0:
        return "up"
    if gain < 0:
        return "down"
    return "flat"

def group_by_yin20(stocks: list[dict[str, Any]]):
    groups = {count: [] for count in range(5)}
    for stock in stocks:
        count = safe_int(stock.get("yin_count_20"), 99)
        if count in groups:
            groups[count].append(stock)
    for group in groups.values():
        group.sort(key=lambda stock: (-stock_gain(stock), str(stock.get("code") or "")))
    return groups

def build_five_priority_rows(stocks: list[dict[str, Any]], include_stock_codes: bool = False):
    groups = {
        field: sorted(
            (stock for stock in stocks if truthy(stock.get(field))),
            key=lambda stock: (
                safe_int(stock.get("yin_count_10d"), 99),
                -stock_gain(stock),
                str(stock.get("code") or ""),
            ),
        )
        for field, _title in FIVE_PRIORITY_FIELDS
    }
    columns = []
    for column, (field, _title) in enumerate(FIVE_PRIORITY_FIELDS):
        cells = []
        by_count = {}
        for stock in groups[field]:
            count = safe_int(stock.get("yin_count_10d"), 99)
            count = count if 0 <= count <= 10 else 99
            by_count.setdefault(count, []).append(stock)
        for count in sorted(by_count):
            selected = by_count[count]
            label = str(count) if count != 99 else "未知"
            cells.append(("%s%s（%d只）" % (FIVE_PRIORITY_GROUP_PREFIX, label, len(selected)), None))
            cells.extend((format_stock_cell(stock, column), stock) for stock in selected)
        columns.append(cells)
    maximum = max((len(column) for column in columns), default=0)
    rows = []
    stock_codes = []
    for offset in range(maximum):
        row = [""] * COLS
        row_codes = [""] * COLS
        for column, cells in enumerate(columns):
            if offset < len(cells):
                row[column] = cells[offset][0]
                stock = cells[offset][1]
                if stock:
                    row_codes[column] = str(stock.get("code") or stock.get("stock_code") or "")
        rows.append(row)
        stock_codes.append(row_codes)
    counts = {field: len(group) for field, group in groups.items()}
    return (rows, counts, stock_codes) if include_stock_codes else (rows, counts)

def prepare_ths_boards(payload: dict[str, Any]):
    boards = []
    for raw_board in payload.get("boards") or []:
        if not isinstance(raw_board, dict):
            continue
        name = str(raw_board.get("board_name") or "").strip()
        if not name:
            continue
        change = safe_float(raw_board.get("change_percent"))
        limit_up_count = max(0, safe_int(raw_board.get("limit_up_count")))
        boards.append({
            "board_name": name,
            "rank": safe_int(raw_board.get("rank"), 999999),
            "change_percent": change,
            "change_percent_text": "%+.2f%%" % change,
            "limit_up_count": limit_up_count,
            "highlight": limit_up_count >= 5,
        })
    boards.sort(key=lambda board: (
        board["rank"], -board["change_percent"], board["board_name"]
    ))
    return boards

def normalize_stock_code(value: Any) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""

def cls_stock_code(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{1,6}", text) and int(text) > 0:
        return text.zfill(6)
    if isinstance(value, float) and value.is_integer() and 0 < value < 1000000:
        return str(int(value)).zfill(6)
    return ""

def prepare_cls_stock_links(
    first_values: list[list[Any]], second_values: list[list[Any]], sheet_url: str,
) -> list[tuple[str, list[dict[str, Any]], bool]]:
    codes_by_name: dict[str, set[str]] = {}
    for row in second_values:
        if len(row) < 2:
            continue
        name = normalize_stock_name(hyperlink_display_text(row[0]))
        code = cls_stock_code(row[1])
        if name and code:
            codes_by_name.setdefault(name, set()).add(code)
    names = sorted((name for name, codes in codes_by_name.items() if len(codes) == 1),
                   key=len, reverse=True)
    updates = []
    for row_index, row in enumerate(first_values, start=1):
        for column, value in enumerate(row[:COLS]):
            if not isinstance(value, list) or not value or not all(
                isinstance(segment, dict) for segment in value
            ):
                continue
            if not any(segment.get("text", "").strip() == "涨停" for segment in value):
                continue
            first = value[0]
            prefix = str(first.get("text") or "")
            match = next((name for name in names if prefix.startswith(name)
                          and (len(prefix) == len(name) or prefix[len(name)].isspace())), None)
            if match and first.get("type") != "url":
                stock_code = re.search(r"(?<!\d)\d{6}(?!\d)", prefix[len(match):])
                if stock_code and stock_code.group() not in codes_by_name[match]:
                    match = None
            linked = [dict(segment, segmentStyle=dict(segment.get("segmentStyle") or {}))
                      for segment in value]
            if match:
                original = linked.pop(0)
                rest = prefix[len(match):]
                style = dict(original.get("segmentStyle") or {})
                linked.insert(0, {"type": "url", "text": match, "link": sheet_url,
                                  "texts": [{"text": match, "segmentStyle": {
                                      "bold": style.get("bold", True),
                                  }}]})
                if rest:
                    original["type"] = "text"
                    original["text"] = rest
                    original.pop("link", None)
                    original.pop("texts", None)
                    linked.insert(1, original)
            elif first.get("type") == "url" and first.get("link", "").split("?sheet=")[0] == sheet_url.split("?sheet=")[0]:
                linked[0]["type"] = "text"
                linked[0].pop("link", None)
                linked[0].pop("texts", None)
                linked[0]["segmentStyle"] = {"bold": True, "foreColor": "#000000"}
            else:
                continue
            cell = "%s%d" % (chr(ord("A") + column), row_index)
            updates.append((cell, linked, bool(match)))
    return updates

def prepare_ths_related_groups(
    payload: dict[str, Any],
    pz1_stocks: list[dict[str, Any]],
    qy2_stocks: list[dict[str, Any]],
):
    pz1_by_code = {
        normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market")): stock
        for stock in pz1_stocks
        if normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market"))
    }
    qy2_by_code = {
        normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market")): stock
        for stock in qy2_stocks
        if normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market"))
    }

    ths_rows = sorted(
        (row for row in (payload.get("stocks") or []) if isinstance(row, dict)),
        key=lambda row: (
            safe_int(row.get("board_rank"), 999999),
            -safe_float(row.get("change_percent")),
            normalize_stock_code(row.get("code")),
        ),
    )
    ths_by_code = {}
    for row in ths_rows:
        code = normalize_stock_code(row.get("code"))
        if code and code not in ths_by_code:
            ths_by_code[code] = row

    def merge_stock(ths_stock: dict[str, Any], condition_stock: dict[str, Any]):
        merged = dict(condition_stock)
        code = normalize_stock_code(ths_stock.get("code") or condition_stock.get("code"))
        name = str(ths_stock.get("name") or condition_stock.get("name") or code).strip()
        change = safe_float(
            ths_stock.get("change_percent"),
            safe_float(
                condition_stock.get("today_change"),
                safe_float(condition_stock.get("ref_gain")),
            ),
        )
        merged.update({
            "code": code,
            "name": name,
            "today_change": change,
            "ref_gain": change,
            "ths_board_name": str(ths_stock.get("board_name") or "").strip(),
            "ths_board_rank": safe_int(ths_stock.get("board_rank"), 999999),
        })
        return merged

    qy2_matches = []
    five_pull_matches = []
    yin20_lte4_matches = []
    quanyang_10d_matches = []
    for code, ths_stock in ths_by_code.items():
        qy2_stock = qy2_by_code.get(code)
        if qy2_stock is not None and truthy(
            qy2_stock.get("qy2_all_yin_low_gte_ma5_or_eaten_10d", 1)
        ):
            qy2_matches.append(merge_stock(ths_stock, qy2_stock))

        pz1_stock = pz1_by_code.get(code)
        if pz1_stock is not None and (
            truthy(pz1_stock.get("five_pull_yin_pull_next_or_eaten"))
            or truthy(pz1_stock.get("five_pull_yin_pull_next"))
        ):
            five_pull_matches.append(merge_stock(ths_stock, pz1_stock))
        if pz1_stock is not None and safe_int(pz1_stock.get("yin_count_20"), 99) <= 4:
            yin20_lte4_matches.append(merge_stock(ths_stock, pz1_stock))
        if pz1_stock is not None and (
            safe_int(pz1_stock.get("yin_count_10d"), 99) == 0
            or truthy(pz1_stock.get("is_quanyang_10d"))
        ):
            quanyang_10d_matches.append(merge_stock(ths_stock, pz1_stock))

    sort_key = lambda stock: (
        safe_int(stock.get("ths_board_rank"), 999999),
        -stock_gain(stock),
        str(stock.get("code") or ""),
    )
    qy2_matches.sort(key=sort_key)
    five_pull_matches.sort(key=sort_key)
    yin20_lte4_matches.sort(key=sort_key)
    quanyang_10d_matches.sort(key=sort_key)
    return {
        "quanyang": qy2_matches,
        "five_pull_yin_pull_next_or_eaten": five_pull_matches,
        "yin_count_20_lte_4": yin20_lte4_matches,
        "is_quanyang_10d": quanyang_10d_matches,
    }

def build_qy2_group_rows(stocks: list[dict[str, Any]]):
    groups: dict[int, list[dict[str, Any]]] = {}
    for stock in stocks:
        code = normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market"))
        if not code:
            continue
        count = safe_int(stock.get("close_below_ma5_count_10d"), -1)
        if count < 0:
            continue
        high_gain_ok = (truthy(stock["ref_high_gain_ok"])
                        if stock.get("ref_high_gain_ok") is not None
                        else safe_float(stock.get("max_gain"), -1) >= 6.5)
        if count >= 2 and not high_gain_ok:
            continue
        groups.setdefault(count, []).append(stock)

    name_width = max(
        10,
        max((display_units(normalize_stock_name(stock.get("name") or stock.get("code")))
             for selected in groups.values() for stock in selected), default=0),
    )
    rows = [[""] * 8]
    rows.extend([[text] + [""] * 7 for text in (
        "筛选条件",
        "1. qy2：阴线不破MA5或次日阳线反包",
        "2. 跌破=0/1：不限参考日最高涨幅",
        "3. 跌破≥2：参考日最高涨幅≥6.5%",
    )])
    rows.append([""] * 8)
    group_offsets = []
    stock_offsets = []
    for count in sorted(groups):
        selected = sorted(groups[count], key=lambda stock: (-stock_gain(stock), normalize_stock_code(stock.get("code"))))
        group_offsets.append(len(rows))
        rows.append(["跌破5日线=%d（%d只）" % (count, len(selected)), "", "", "", "", "", "", ""])
        for stock in selected:
            name = normalize_stock_name(stock.get("name") or stock.get("code"))
            code = normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market"))
            gap = name_width + 2 - display_units(name)
            padding = "　" * (gap // 2) + " " * (gap % 2)
            gain = stock_gain(stock)
            gain_color = "#D93025" if gain > 0 else "#188038" if gain < 0 else "#2E2A35"
            no_touch = safe_int(stock.get("continuous_4_no_touch")) == 1
            shadow_sum = stock.get("lower_shadow_pct_sum_4d")
            stock_prefix = "%s%s%s\u3000  %+.2f%%" % (name, padding, code, gain)
            stock_offsets.append((len(rows), gain))
            rows.append([[
                {"type": "text", "text": "%s%s%s\u3000  " % (name, padding, code),
                 "segmentStyle": {"bold": True, "foreColor": "#000000"}},
                {"type": "text", "text": "%+.2f%%" % gain,
                 "segmentStyle": {"bold": False, "foreColor": gain_color}},
                *stock_tag_segments(
                    stock_prefix, 0,
                    safe_int(stock.get("is_limit_up")) == 1,
                    safe_int(stock.get("is_bare_foot")) == 1,
                ),
            ],
                "满足连续4天不碰5日线" if no_touch else "",
                ("4天不碰5日线累计下引线和：%.2f%%" % safe_float(shadow_sum)
                 if shadow_sum is not None else "4天不碰5日线累计下引线和：暂无数据")
                if no_touch else "",
                "", "", "", "", "",
            ])
    return rows, group_offsets, stock_offsets

def build_yin_ma5_group_rows(stocks: list[dict[str, Any]]):
    groups: dict[int, list[dict[str, Any]]] = {}
    for stock in stocks:
        code = normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market"))
        yin_count = safe_int(stock.get("yin_count_20"), -1)
        below_count = safe_int(stock.get("close_below_ma5_count_20d"), -1)
        if code and 0 <= yin_count <= 6 and 0 <= below_count <= 1:
            groups.setdefault(yin_count, []).append(stock)

    name_width = max(10, max(
        (display_units(normalize_stock_name(stock.get("name") or stock.get("code")))
         for selected in groups.values() for stock in selected), default=0,
    ))
    rows: list[list[Any]] = []
    group_offsets: list[int] = []
    for yin_count in sorted(groups):
        selected = sorted(groups[yin_count], key=lambda stock: (
            -stock_gain(stock), normalize_stock_code(stock.get("code")),
        ))
        group_offsets.append(len(rows))
        rows.append(["20天阴线=%d（%d只）" % (yin_count, len(selected))] + [""] * 7)
        for stock in selected:
            name = normalize_stock_name(stock.get("name") or stock.get("code"))
            code = normalize_stock_code(stock.get("code") or stock.get("stock_code_with_market"))
            gap = name_width + 2 - display_units(name)
            padding = "\u3000" * (gap // 2) + " " * (gap % 2)
            gain = stock_gain(stock)
            color = "#D93025" if gain > 0 else "#188038" if gain < 0 else "#2E2A35"
            rows.append([[
                {"type": "text", "text": "%s%s%s\u3000  " % (name, padding, code),
                 "segmentStyle": {"bold": True, "foreColor": "#000000"}},
                {"type": "text", "text": "%+.2f%%" % gain,
                 "segmentStyle": {"foreColor": color}},
            ]] + [""] * 7)
    return rows, group_offsets

def default_manual_blocks():
    return {
        "review": [
            ["涨停复盘", "4天强", "一字横盘", "厂字横盘", ""],
            ["", "几乎光头", "", "", ""],
            ["", "几乎光脚", "", "", ""],
            ["", "十字星 上下引线很多", "", "", ""],
        ],
        "strong_k": [[
            "4. 阴线后面强势K",
            "几乎光头",
            "几乎光脚",
            "十字星 上下引线很多",
            "中体产业 吃亏",
        ]],
        "final": [
            ["Final", "", "", "", ""],
            ["", "4天强", "几乎光头,几乎光脚,十字星 上下引线很多", "阴线后面阳线，不要出现2连阴 除非阴线也很强", ""],
            ["", 3, 7, "", ""],
        ],
    }

def normalize_rows(values: list[list[Any]], rows: int, cols: int = COLS):
    return [
        [
            values[row][column] if row < len(values) and column < len(values[row]) else ""
            for column in range(cols)
        ]
        for row in range(rows)
    ]

def build_sheet_content(
    ths_payload: dict[str, Any],
    stocks: list[dict[str, Any]],
    manual_blocks: dict[str, list[list[Any]]] | None = None,
    qy2_stocks: list[dict[str, Any]] | None = None,
    related_pz_stocks: list[dict[str, Any]] | None = None,
):
    boards = prepare_ths_boards(ths_payload)
    ths_related_groups = prepare_ths_related_groups(
        ths_payload, related_pz_stocks or stocks, qy2_stocks or []
    )
    low_yin = [stock for stock in stocks if safe_int(stock.get("yin_count_20"), 99) <= 4]
    high_gain = [
        stock for stock in low_yin
        if truthy(stock.get("ref_high_gain_ok")) or safe_float(stock.get("max_gain")) >= 6.5
    ]
    continuous_4 = [
        stock for stock in low_yin if safe_int(stock.get("continuous_4_no_touch")) == 1
    ]
    quanyang = [stock for stock in stocks if safe_int(stock.get("yin_count_10d"), 99) == 0]
    quanyang.sort(key=lambda stock: (-stock_gain(stock), str(stock.get("code") or "")))
    manual = manual_blocks or default_manual_blocks()

    rows: list[list[Any]] = []
    layout: dict[str, Any] = {
        "major_titles": [],
        "sub_titles": [],
        "plain_headers": [],
        "red_headers": [],
        "board_hot_rows": [],
        "up_cells": [],
        "down_cells": [],
        "red_ranges": [],
        "purple_ranges": [],
        "link_cells": [],
        "header_link_cells": [],
        "yellow_link_cells": [],
        "related_name_cells": [],
        "related_gain_up_cells": [],
        "related_gain_down_cells": [],
        "related_gain_flat_cells": [],
        "five_group_cells": [],
        "stock_codes": {},
        "regions": [],
    }

    def row_number():
        return len(rows) + 1

    def add_blanks(count: int = 2):
        rows.extend([["", "", "", "", ""] for _ in range(count)])

    def focus_target_url(page: int):
        ref_date = ""
        for stock in stocks:
            digits = "".join(
                char for char in str(stock.get("ref_date") or "") if char.isdigit()
            )
            if len(digits) == 8:
                ref_date = "%s-%s-%s" % (digits[:4], digits[4:6], digits[6:8])
                break
        target_url = "%s?page=%d" % (FOCUS_PAGE_URL, page)
        if ref_date:
            target_url += "&date=%s" % ref_date
        return target_url

    def track_stock(cell: str, stock: dict[str, Any]):
        code = str(stock.get("code") or stock.get("stock_code") or "").strip()
        if code:
            layout["stock_codes"][cell] = code
        if stock_gain_direction(stock) == "up":
            layout["up_cells"].append(cell)
        elif stock_gain_direction(stock) == "down":
            layout["down_cells"].append(cell)

    def add_board_section():
        title_row = row_number()
        rows.append(["同花顺板块", "", "", "", ""])
        layout["major_titles"].append(title_row)
        header_row = row_number()
        rows.append(["板块名称", "板块涨跌幅", "板块涨停家数", "", ""])
        layout["plain_headers"].append(header_row)
        data_start = row_number()
        for board in boards:
            current = row_number()
            rows.append([
                board["board_name"], board["change_percent_text"], board["limit_up_count"], "", ""
            ])
            if board["highlight"]:
                layout["board_hot_rows"].append(current)
        layout["regions"].append({
            "title": "同花顺板块",
            "title_row": title_row,
            "header_row": header_row,
            "data_start_row": data_start,
            "data_end_row": row_number() - 1,
            "count": len(boards),
            "highlight_count": sum(1 for board in boards if board["highlight"]),
        })

    def add_single_list(title: str, selected: list[dict[str, Any]]):
        title_row = row_number()
        rows.append([title, "", "", "", ""])
        layout["major_titles"].append(title_row)
        data_start = row_number()
        for stock in selected:
            current = row_number()
            rows.append([format_stock_cell(stock), "", "", "", ""])
            track_stock("A%d" % current, stock)
        layout["regions"].append({
            "title": title,
            "title_row": title_row,
            "data_start_row": data_start,
            "data_end_row": row_number() - 1,
            "count": len(selected),
        })

    def add_quanyang_link_section():
        title_row = row_number()
        target_url = focus_target_url(5)
        rows.append([
            "1全阳",
            hyperlink_value(target_url, FOCUS_LINK_TEXT),
            "", "", "",
        ])
        layout["major_titles"].append(title_row)
        layout["link_cells"].append("B%d" % title_row)
        layout["regions"].append({
            "title": "1全阳",
            "title_row": title_row,
            "data_end_row": title_row,
            "count": 0,
            "link_url": target_url,
        })

    def add_ths_related_section():
        title_row = row_number()
        rows.append(["同花顺板块相关个股", "", "", "", "", "", "", ""])
        layout["major_titles"].append(title_row)
        header_row = row_number()
        rows.append([
            "全阳", "",
            "5拉阴后拉或吃", "",
            "20阴<=4", "",
            "无阴线（全阳）", "",
        ])
        layout["plain_headers"].append(header_row)
        quanyang_stocks = ths_related_groups["quanyang"]
        five_pull_stocks = ths_related_groups["five_pull_yin_pull_next_or_eaten"]
        yin20_lte4_stocks = ths_related_groups["yin_count_20_lte_4"]
        no_yin_stocks = ths_related_groups["is_quanyang_10d"]
        selected_groups = (
            quanyang_stocks,
            five_pull_stocks,
            yin20_lte4_stocks,
            no_yin_stocks,
        )
        selected_stocks = [stock for group in selected_groups for stock in group]
        name_width = max((display_units(normalize_stock_name(stock.get("name")))
                          for stock in selected_stocks), default=0)
        board_width = max((display_units(stock.get("ths_board_name"))
                           for stock in selected_stocks), default=0)

        def pad_to_width(value: str, width: int) -> str:
            gap = max(2, width + 2 - display_units(value))
            return value + "　" * (gap // 2) + " " * (gap % 2)

        maximum = max((len(group) for group in selected_groups), default=0)
        data_start = row_number()
        for offset in range(maximum):
            data_row_number = row_number()
            data_row = [""] * COLS
            for group_index, selected in enumerate(selected_groups):
                if offset >= len(selected):
                    continue
                stock = selected[offset]
                board_name = str(stock.get("ths_board_name") or "").strip()
                name = normalize_stock_name(stock.get("name") or stock.get("code") or "")
                name_column = group_index * 2
                prefix = pad_to_width(name, name_width) + pad_to_width(board_name, board_width)
                gain = stock_gain(stock)
                gain_color = "#D93025" if gain > 0 else "#188038" if gain < 0 else "#000000"
                gain_text = "%+.2f%%" % gain
                data_row[name_column] = [
                    {"type": "text", "text": prefix,
                     "segmentStyle": {"bold": True, "foreColor": "#000000"}},
                    {"type": "text", "text": gain_text,
                     "segmentStyle": {"bold": True, "foreColor": gain_color}},
                    *stock_tag_segments(
                        prefix + gain_text, name_column,
                        safe_int(stock.get("is_limit_up")) == 1,
                        safe_int(stock.get("is_bare_foot")) == 1,
                    ),
                ]
                name_column_name = chr(ord("A") + name_column)
                name_cell = "%s%d" % (name_column_name, data_row_number)
                layout["related_name_cells"].append(name_cell)
                track_stock(name_cell, stock)
            rows.append(data_row)
        layout["regions"].append({
            "title": "同花顺板块相关个股",
            "title_row": title_row,
            "header_row": header_row,
            "data_start_row": data_start,
            "data_end_row": row_number() - 1,
            "counts": {
                "全阳": len(quanyang_stocks),
                "5拉阴后拉或吃": len(five_pull_stocks),
                "20阴<=4": len(yin20_lte4_stocks),
                "无阴线（全阳）": len(no_yin_stocks),
            },
        })

    def add_yin_section(title: str, selected: list[dict[str, Any]], kind: str):
        title_row = row_number()
        rows.append([title, "", "", "", ""])
        layout["major_titles" if kind == "major" else "sub_titles"].append(title_row)
        header_row = row_number()
        rows.append(["20天阴线=%d" % count for count in range(5)])
        layout["plain_headers"].append(header_row)
        groups = group_by_yin20(selected)
        maximum = max((len(group) for group in groups.values()), default=0)
        data_start = row_number()
        for offset in range(maximum):
            current = row_number()
            row = []
            for column in range(5):
                if offset >= len(groups[column]):
                    row.append("")
                    continue
                stock = groups[column][offset]
                row.append(format_stock_cell(stock, column))
                track_stock("%s%d" % (chr(ord("A") + column), current), stock)
            rows.append(row)
        layout["regions"].append({
            "title": title,
            "title_row": title_row,
            "header_row": header_row,
            "data_start_row": data_start,
            "data_end_row": row_number() - 1,
            "count": len(selected),
            "group_counts": {str(count): len(groups[count]) for count in range(5)},
        })

    def add_yin4_link_section():
        title_row = row_number()
        target_url = focus_target_url(1)
        rows.append([
            YIN4_SECTION_TITLE,
            hyperlink_value(target_url, YIN4_LINK_TEXT),
            "", "", "",
        ])
        rows.extend([[note, "", "", "", ""] for note in YIN4_NOTES])
        layout["major_titles"].append(title_row)
        layout["link_cells"].append("B%d" % title_row)
        layout["regions"].append({
            "title": YIN4_SECTION_TITLE,
            "title_row": title_row,
            "data_end_row": row_number() - 1,
            "count": len(low_yin),
            "link_url": target_url,
        })

    def add_five_priority_section():
        title_row = row_number()
        rows.append(["3.5拉升", "", "", "", ""])
        layout["major_titles"].append(title_row)
        header_row = row_number()
        rows.append([title for _field, title in FIVE_PRIORITY_FIELDS] + [""] * (COLS - 2))
        layout["plain_headers"].append(header_row)
        data_start = row_number()
        data_rows, counts, data_stock_codes = build_five_priority_rows(
            stocks, include_stock_codes=True,
        )
        for offset, row in enumerate(data_rows):
            for column, value in enumerate(row[:2]):
                if isinstance(value, str) and value.startswith(FIVE_PRIORITY_GROUP_PREFIX):
                    layout["five_group_cells"].append("%s%d" % (chr(ord("A") + column), data_start + offset))
                code = data_stock_codes[offset][column] if offset < len(data_stock_codes) else ""
                if code:
                    layout["stock_codes"]["%s%d" % (chr(ord("A") + column), data_start + offset)] = code
        rows.extend(data_rows)
        layout["regions"].append({
            "title": "3.5拉升",
            "title_row": title_row,
            "header_row": header_row,
            "data_start_row": data_start,
            "data_end_row": row_number() - 1,
            "counts": counts,
        })

    def add_stable_before_limit_link_section():
        title_row = row_number()
        target_url = focus_target_url(3)
        rows.append([
            hyperlink_value(target_url, "3.6涨停前稳定（点击跳转）"),
            "", "", "", "",
        ])
        layout["major_titles"].append(title_row)
        layout["yellow_link_cells"].append("A%d" % title_row)
        layout["regions"].append({
            "title": "3.6涨停前稳定",
            "title_row": title_row,
            "data_end_row": title_row,
            "link_url": target_url,
        })

    def add_manual_region(title: str, block: list[list[Any]]):
        start = row_number()
        normalized = normalize_rows(block, len(block))
        rows.extend(normalized)
        layout["regions"].append({
            "title": title,
            "title_row": start,
            "data_end_row": row_number() - 1,
            "row_count": len(normalized),
        })

    add_board_section()
    add_blanks()
    add_ths_related_section()
    add_blanks()
    add_quanyang_link_section()
    add_blanks()
    add_yin4_link_section()
    add_blanks(3)
    add_yin_section("条件1 ZDZF8(拉伸6.5)", high_gain, "sub")
    add_blanks()
    add_yin_section("条件2 4不碰", continuous_4, "sub")
    add_blanks()
    add_five_priority_section()
    add_blanks()
    add_stable_before_limit_link_section()
    add_blanks()
    add_manual_region("涨停复盘", manual["review"])
    review_start = layout["regions"][-1]["title_row"]
    layout["red_ranges"].append("A%d:A%d" % (review_start, review_start))
    layout["purple_ranges"].extend([
        "B%d:D%d" % (review_start, review_start),
        "B%d:B%d" % (review_start + 1, review_start + 3),
    ])
    add_blanks()
    add_manual_region("4. 阴线后面强势K", manual["strong_k"])
    strong_start = layout["regions"][-1]["title_row"]
    layout["red_ranges"].append("A%d:A%d" % (strong_start, strong_start))
    layout["purple_ranges"].append("B%d:E%d" % (strong_start, strong_start))
    add_blanks()
    add_manual_region("Final", manual["final"])
    final_start = layout["regions"][-1]["title_row"]
    layout["red_ranges"].append("A%d:A%d" % (final_start, final_start))
    layout["purple_ranges"].append("B%d:D%d" % (final_start + 1, final_start + 1))
    return rows, layout

def last_nonempty_row(values: list[list[Any]]) -> int:
    for index in range(len(values) - 1, -1, -1):
        if any(value not in (None, "") for value in values[index]):
            return index + 1
    return 0

def style_payload(sheet_id: str, rows: int, layout: dict[str, Any]):
    body = {
        "font": {"bold": False, "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#000000", "backColor": "#FFFFFF", "clean": False,
    }
    major = {
        "font": {"bold": True, "fontSize": "13pt/1.5", "clean": False},
        "hAlign": 1, "vAlign": 1, "foreColor": "#FFFF00", "backColor": "#FF0000", "clean": False,
    }
    purple = {
        "font": {"bold": False, "fontSize": "13pt/1.5", "clean": False},
        "hAlign": 1, "vAlign": 1, "foreColor": "#FFFFFF", "backColor": "#9A38D7", "clean": False,
    }
    header = {
        "font": {"bold": True, "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 1, "vAlign": 1, "foreColor": "#000000", "backColor": "#FFFFFF", "clean": False,
    }
    up = {
        "font": {"bold": False, "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#D93025", "clean": False,
    }
    down = {
        "font": {"bold": False, "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#188038", "clean": False,
    }
    board_hot = {
        "font": {"bold": True, "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#FFFFFF", "backColor": "#9A38D7", "clean": False,
    }
    board_title = dict(major, hAlign=0)
    board_header = dict(header, hAlign=0)
    link = {
        "font": {"bold": True, "fontSize": "12pt/1.5", "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#D93025", "backColor": "#FFFFFF", "clean": False,
    }
    header_link = {
        "font": {"bold": True, "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 1, "vAlign": 1, "foreColor": "#D93025", "backColor": "#FFFFFF", "clean": False,
    }
    yellow_link = {
        "font": {"bold": True, "fontSize": "12pt/1.5", "clean": False},
        "hAlign": 1, "vAlign": 1, "foreColor": "#FFFF00", "backColor": "#FF0000", "clean": False,
    }
    related_name = {
        "font": {"bold": True, "fontFamily": "NSimSun", "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#000000", "clean": False,
    }
    related_gain_up = dict(related_name, foreColor="#D93025")
    related_gain_down = dict(related_name, foreColor="#188038")
    related_gain_flat = dict(related_name, foreColor="#000000")

    def qualify(cell_range: str):
        return "%s!%s" % (sheet_id, cell_range)

    groups = [{"ranges": [qualify("A1:%s%d" % (PRIMARY_END_COLUMN, rows))], "style": body}]

    def add(ranges, style):
        if ranges:
            groups.append({"ranges": [qualify(value) for value in ranges], "style": style})

    add(["A%d:A%d" % (row, row) for row in layout["major_titles"]], major)
    add(["A%d:A%d" % (row, row) for row in layout["sub_titles"]], purple)
    add(["A%d:%s%d" % (row, PRIMARY_END_COLUMN, row) for row in layout["plain_headers"]], header)
    add(["A%d:%s%d" % (row, PRIMARY_END_COLUMN, row) for row in layout["red_headers"]], major)
    add(["A%d:C%d" % (row, row) for row in layout["board_hot_rows"]], board_hot)
    add(["%s:%s" % (cell, cell) for cell in layout["up_cells"]], up)
    add(["%s:%s" % (cell, cell) for cell in layout["down_cells"]], down)
    add(layout["red_ranges"], major)
    add(layout["purple_ranges"], purple)
    add(["%s:%s" % (cell, cell) for cell in layout["link_cells"]], link)
    add(["%s:%s" % (cell, cell) for cell in layout["header_link_cells"]], header_link)
    add(["%s:%s" % (cell, cell) for cell in layout["yellow_link_cells"]], yellow_link)
    add(["%s:%s" % (cell, cell) for cell in layout["related_name_cells"]], related_name)
    add(["%s:%s" % (cell, cell) for cell in layout["related_gain_up_cells"]], related_gain_up)
    add(["%s:%s" % (cell, cell) for cell in layout["related_gain_down_cells"]], related_gain_down)
    add(["%s:%s" % (cell, cell) for cell in layout["related_gain_flat_cells"]], related_gain_flat)
    add(["%s:%s" % (cell, cell) for cell in layout.get("five_group_cells", [])], {
        "font": {"fontFamily": "Microsoft YaHei", "bold": True, "fontSize": "11pt/1.5", "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#51336C", "backColor": "#F5F0F8", "clean": False,
    })
    board_region = next(
        (region for region in layout["regions"] if region.get("title") == "同花顺板块"),
        None,
    )
    if board_region:
        add(["A%d:A%d" % (board_region["title_row"], board_region["title_row"])], board_title)
        add(["A%d:C%d" % (board_region["header_row"], board_region["header_row"])], board_header)
    return groups

def _style_template_data(
    feishu, token: str, book: str, sheet_id: str, start: int,
    data_rows: list[list[Any]], kind: str,
    *, source_start: int = 0, source_end: int = -1,
    source_layout: dict[str, Any] | None = None,
    boards: list[dict[str, Any]] | None = None,
    qy2_groups: list[int] | None = None,
    qy2_stocks: list[tuple[int, float]] | None = None,
) -> None:
    if not data_rows:
        return
    end = start + len(data_rows) - 1
    groups = [{"ranges": ["%s!A%d:H%d" % (sheet_id, start, end)], "style": {
        "font": {"fontFamily": "Microsoft YaHei", "fontSize": "11pt/1.5", "bold": False, "clean": False},
        "hAlign": 0, "vAlign": 1, "foreColor": "#2E2A35", "backColor": "#FFFFFF", "clean": False,
    }}]

    def add(cells: list[str], style: dict[str, Any]):
        if cells:
            groups.append({"ranges": ["%s!%s:%s" % (sheet_id, cell, cell) for cell in cells],
                           "style": dict(style, clean=False)})

    up = {"foreColor": "#A94A51"}
    down = {"foreColor": "#2A7556"}
    if kind == "qy2":
        groups.append({"ranges": ["%s!A%d:A%d" % (sheet_id, start + 1, start + 1)], "style": {
            "font": {"bold": True, "fontSize": "11pt/1.5", "clean": False},
            "hAlign": 0, "foreColor": "#3E354B", "backColor": "#F1EFF4", "clean": False,
        }})
        groups.append({"ranges": ["%s!A%d:A%d" % (sheet_id, start + 2, start + 4)], "style": {
            "font": {"bold": False, "fontSize": "10pt/1.5", "clean": False},
            "hAlign": 0, "foreColor": "#3E354B", "clean": False,
        }})
        for offset in qy2_groups or []:
            row = start + offset
            groups.append({"ranges": ["%s!A%d:H%d" % (sheet_id, row, row)], "style": {
                "font": {"bold": True, "fontSize": "11pt/1.5", "clean": False},
                "hAlign": 0, "foreColor": "#51336C", "backColor": "#F5F0F8", "clean": False,
            }})
        add(["A%d" % (start + offset) for offset, _gain in qy2_stocks or []],
            {"font": {"fontFamily": "NSimSun", "clean": False}, "hAlign": 0})
        no_touch_rows = [start + offset for offset, row in enumerate(data_rows)
                         if row[1] == "满足连续4天不碰5日线"]
        add(["B%d" % row for row in no_touch_rows],
            {"font": {"bold": True, "clean": False}, "foreColor": "#D93025", "hAlign": 0})
        add(["C%d" % row for row in no_touch_rows],
            {"font": {"bold": True, "clean": False}, "foreColor": "#7C3AED", "hAlign": 0})
    elif kind == "yin_ma5":
        for offset in qy2_groups or []:
            row = start + offset
            groups.append({"ranges": ["%s!A%d:H%d" % (sheet_id, row, row)], "style": {
                "font": {"bold": True, "fontSize": "11pt/1.5", "clean": False},
                "hAlign": 0, "foreColor": "#51336C", "backColor": "#F5F0F8", "clean": False,
            }})
    elif kind == "boards":
        for offset, board in enumerate(boards or []):
            if board["highlight"]:
                row = start + offset
                groups.append({"ranges": ["%s!A%d:C%d" % (sheet_id, row, row)], "style": {
                    "font": {"bold": True, "clean": False},
                    "foreColor": "#51336C", "backColor": "#DCC8EA", "clean": False,
                }})
        add(["B%d" % (start + offset) for offset, board in enumerate(boards or [])
             if board["change_percent"] > 0], up)
        add(["B%d" % (start + offset) for offset, board in enumerate(boards or [])
             if board["change_percent"] < 0], down)
    elif kind == "five_priority":
        groups.append({"ranges": ["%s!A%d:B%d" % (sheet_id, start, end)], "style": {
            "font": {"fontFamily": "NSimSun", "clean": False},
            "hAlign": 0, "clean": False,
        }})
        header_cells = [
            "%s%d" % (chr(ord("A") + column), start + offset)
            for offset, row in enumerate(data_rows)
            for column, value in enumerate(row[:2])
            if isinstance(value, str) and value.startswith(FIVE_PRIORITY_GROUP_PREFIX)
        ]
        add(header_cells, {
            "font": {"fontFamily": "Microsoft YaHei", "bold": True, "fontSize": "11pt/1.5", "clean": False},
            "hAlign": 0, "foreColor": "#51336C", "backColor": "#F5F0F8",
        })
    else:
        layout = source_layout or {}
        def mapped(key: str):
            result = []
            for cell in layout.get(key, []):
                match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
                if match and source_start <= int(match.group(2)) <= source_end:
                    result.append("%s%d" % (match.group(1), start + int(match.group(2)) - source_start))
            return result

        add(mapped("related_name_cells"),
            {"font": {"bold": True, "fontFamily": "NSimSun", "clean": False}, "hAlign": 0})
        add(mapped("related_gain_up_cells") + mapped("up_cells"), up)
        add(mapped("related_gain_down_cells") + mapped("down_cells"), down)
    limit_tag_cells = [
        "%s%d" % (chr(ord("A") + column), start + offset)
        for offset, row in enumerate(data_rows)
        for column, value in enumerate(row)
        if isinstance(value, list) and any(
            isinstance(segment, dict) and segment.get("text", "").strip() in ("涨停", "几乎光脚")
            for segment in value
        )
    ]
    add(limit_tag_cells, {"font": {"fontFamily": "NSimSun", "clean": False}, "hAlign": 0})
    feishu._json_request(
        "PUT", "/sheets/v2/spreadsheets/%s/styles_batch_update" % book,
        token=token, body={"data": groups},
    )
    feishu._json_request(
        "PUT", "/sheets/v2/spreadsheets/%s/dimension_range" % book,
        token=token,
        body={"dimension": {"sheetId": sheet_id, "majorDimension": "ROWS",
                            "startIndex": start, "endIndex": end},
              "dimensionProperties": {"visible": True, "fixedSize": 31}},
    )

def _restore_stock_gain_segments(
    feishu, token: str, book: str, sheet_id: str, start: int,
    data_rows: list[list[Any]],
) -> None:
    ranges = []
    for row_offset, row in enumerate(data_rows):
        for column, value in enumerate(row):
            if isinstance(value, list):
                if not value or not all(
                    isinstance(segment, dict) and segment.get("type") == "text"
                    for segment in value
                ):
                    continue
                rich_value = value
            elif isinstance(value, str):
                match = re.fullmatch(r"(.*?)([+-]\d+(?:\.\d+)?%)", value)
                if not match:
                    continue
                prefix, gain_text = match.groups()
                gain = safe_float(gain_text[:-1])
                gain_color = "#D93025" if gain > 0 else "#188038" if gain < 0 else "#000000"
                rich_value = []
                if prefix:
                    rich_value.append({"type": "text", "text": prefix,
                                       "segmentStyle": {"foreColor": "#000000"}})
                rich_value.append({"type": "text", "text": gain_text,
                                   "segmentStyle": {"foreColor": gain_color}})
            else:
                continue
            if (not isinstance(value, list)
                    and not re.search(r"[+-]\d+(?:\.\d+)?%$", hyperlink_display_text(rich_value))):
                continue
            cell = "%s%d" % (chr(ord("A") + column), start + row_offset)
            ranges.append({"range": "%s!%s:%s" % (sheet_id, cell, cell),
                           "values": [[rich_value]]})
    for offset in range(0, len(ranges), 200):
        feishu._json_request(
            "POST", "/sheets/v2/spreadsheets/%s/values_batch_update" % book,
            token=token, body={"valueRanges": ranges[offset:offset + 200]},
        )
