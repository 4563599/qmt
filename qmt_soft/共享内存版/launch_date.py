"""Date validation shared by the desktop launcher and external workers."""
from datetime import date, datetime, timedelta, timezone
import re


def china_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def parse_date(value, today=None):
    today = today or china_today()
    text = value.strip() if isinstance(value, str) else ''
    if not re.fullmatch(r'\d{8}|\d{4}-\d{2}-\d{2}', text):
        raise ValueError('请输入有效日期，例如 2026-10-23 或 20261023。')
    try:
        result = datetime.strptime(text.replace('-', ''), '%Y%m%d').date()
    except ValueError:
        raise ValueError('日期不存在，请检查年月日。') from None
    if result > today:
        raise ValueError('不能选择未来日期。')
    return result


def selection_reference(value, today=None):
    today = today or china_today()
    selected = parse_date(value, today)
    return '' if selected == today else selected.strftime('%Y%m%d')


def normalize_reference(value):
    if value is None or value == '':
        return value
    return parse_date(value).strftime('%Y%m%d')
