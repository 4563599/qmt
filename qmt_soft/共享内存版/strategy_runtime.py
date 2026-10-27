"""External-only strategy settings; original calculations and time rules stay intact."""

from datetime import datetime
import math
import re

from qmt_shm_bridge import BridgeError


OPTION_NAMES = {
    'panzhong': frozenset((
        'MIN_REALTIME_GAIN_PCT', 'HIGH_GAIN_THRESHOLD_PCT', 'MAX_YIN_COUNT_20',
        'MIN_HIGH_GAIN_COUNT_10D', 'MIN_LOW_ABOVE_MA5_COUNT_10D', 'PZ1_MAX_YIN_COUNT_20',
        'HISTORY_REQUEST_DAYS', 'KLINE_TOTAL_DAYS', 'HISTORY_BATCH_SIZE', 'TIMER_INTERVAL_TEXT',
        'REDIS_TTL_SEC', 'DINGTALK_ENABLED', 'DINGTALK_RETRY_COUNT',
        'DINGTALK_PERSIST_RETRY_COUNT', 'DINGTALK_PERSIST_RETRY_MAX_DELAY_SEC',
        'DINGTALK_MAX_YIN_COUNT_20', 'LOG_ENABLED', 'LOG_SELECTED_LIMIT',
        'LOG_BASE_DIR', 'STRATEGY_DIR_NAME',
    )),
    'ths': frozenset((
        'REDIS_QMT_KLINE_DAYS', 'REDIS_QMT_KLINE_TTL_SEC', 'REDIS_STATUS_TTL_SEC',
        'HISTORY_BATCH_SIZE', 'HISTORY_REQUEST_DAYS', 'PUBLISH_INTERVAL_TEXT',
        'CLOSE_BAR_REFRESH_SEC', 'LOG_BASE_DIR', 'STRATEGY_DIR_NAME',
    )),
}


def bridge_config(status):
    config = status.get('strategy_config') if isinstance(status, dict) else None
    if config is None:
        return None
    if not isinstance(config, dict):
        raise BridgeError('Invalid bridge strategy configuration')
    return config


def strategy_kind(label):
    return 'ths' if label == 'THS' or label == 'ths' else 'panzhong'


def enabled_sources(sources, status):
    config = bridge_config(status) or {}
    switches = config.get('enabled', {})
    if (not isinstance(switches, dict) or set(switches) - set(OPTION_NAMES)
            or any(type(v) is not bool for v in switches.values())):
        raise BridgeError('Invalid strategy enabled switches')
    selected = {label: source for label, source in sources.items()
                if switches.get(strategy_kind(label), True)}
    if not selected:
        raise BridgeError('All selected strategies are disabled in the QMT bridge')
    return selected


def interval_seconds(value):
    match = re.fullmatch(r'([1-9]\d{0,3})nSecond', value) if isinstance(value, str) else None
    if not match or int(match.group(1)) > 3600:
        raise BridgeError('Timer interval must be 1nSecond through 3600nSecond')
    return int(match.group(1))


def apply_source_options(module, kind, config):
    """Validate the whole change set before mutating this private module instance."""
    if not config or config.get('version', 1) == 1:
        return {}
    if config.get('version') != 2:
        raise BridgeError('Unsupported bridge configuration version')
    groups = config.get('options', {})
    if not isinstance(groups, dict) or set(groups) - set(OPTION_NAMES):
        raise BridgeError('Unknown strategy options group')
    options = groups.get(kind, {})
    if not isinstance(options, dict) or set(options) - OPTION_NAMES[kind]:
        raise BridgeError('Unknown or protected strategy option')
    changes = {}
    reference = config.get('REFERENCE_DATE')
    if reference is not None:
        if not isinstance(reference, str):
            raise BridgeError('REFERENCE_DATE must be empty or YYYYMMDD')
        if reference:
            try:
                parsed = datetime.strptime(reference, '%Y%m%d')
                if parsed.strftime('%Y%m%d') != reference:
                    raise ValueError()
            except ValueError:
                raise BridgeError('REFERENCE_DATE is not a valid YYYYMMDD date')
        changes['REFERENCE_DATE'] = reference
    for name, value in options.items():
        if value is None:
            continue
        if not hasattr(module, name):
            raise BridgeError('Original strategy does not support option ' + name)
        previous = getattr(module, name)
        valid = False
        if type(previous) is bool:
            valid = type(value) is bool
        elif type(previous) is int:
            minimum = 0 if name in ('MAX_YIN_COUNT_20', 'MIN_HIGH_GAIN_COUNT_10D',
                'MIN_LOW_ABOVE_MA5_COUNT_10D', 'PZ1_MAX_YIN_COUNT_20',
                'DINGTALK_MAX_YIN_COUNT_20', 'LOG_SELECTED_LIMIT') else 1
            valid = type(value) is int and minimum <= value <= 2147483647
        elif type(previous) is float:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        elif isinstance(previous, str):
            valid = isinstance(value, str) and 0 < len(value) <= 500
        if not valid:
            raise BridgeError('Invalid value for strategy option ' + name)
        if name in ('TIMER_INTERVAL_TEXT', 'PUBLISH_INTERVAL_TEXT'):
            interval_seconds(value)
        if name == 'HISTORY_BATCH_SIZE' and value > 10000:
            raise BridgeError('HISTORY_BATCH_SIZE exceeds bridge limits')
        changes[name] = value
    days_name = 'KLINE_TOTAL_DAYS' if kind == 'panzhong' else 'REDIS_QMT_KLINE_DAYS'
    days = changes.get(days_name, getattr(module, days_name, 60))
    if not 25 <= days <= 2048:
        raise BridgeError('K-line length must be between 25 and 2048 days')
    if days_name in changes:
        if kind == 'panzhong':
            changes['KLINE_HISTORY_DAYS'] = days - 1
        if 'HISTORY_REQUEST_DAYS' not in changes:
            changes['HISTORY_REQUEST_DAYS'] = max(getattr(module, 'HISTORY_REQUEST_DAYS', 65), days + 5)
    history = changes.get('HISTORY_REQUEST_DAYS', getattr(module, 'HISTORY_REQUEST_DAYS', 65))
    if not days <= history <= 2048:
        raise BridgeError('History request length must cover K-lines and be at most 2048')
    clocks = config.get('history_reload_times', {})
    if not isinstance(clocks, dict):
        raise BridgeError('Invalid history reload time configuration')
    clock = clocks.get(kind, '09:15:00' if kind == 'panzhong' else '09:25:00')
    if not isinstance(clock, str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d:[0-5]\d', clock):
        raise BridgeError('History reload time must be HH:MM:SS')
    for name, value in changes.items():
        setattr(module, name, value)
    return changes


class StrategySettings:
    def __init__(self, module, kind, config):
        if type(config.get('CHECK_TRADING_TIME')) is not bool:
            raise BridgeError('Bridge CHECK_TRADING_TIME must be True or False')
        self.module, self.kind = module, kind
        apply_source_options(module, kind, config)
        self.reload_time = config.get('history_reload_times', {}).get(
            kind, '09:15:00' if kind == 'panzhong' else '09:25:00')
        self.check_trading_time = None
        self.update_check(config)

    def update_check(self, config):
        setting = config.get('CHECK_TRADING_TIME')
        if type(setting) is not bool:
            raise BridgeError('Bridge CHECK_TRADING_TIME must be True or False')
        if hasattr(self.module, 'CHECK_TRADING_TIME'):
            self.module.CHECK_TRADING_TIME = setting
        if self.check_trading_time is not setting:
            if hasattr(self.module, 'CHECK_TRADING_TIME'):
                print('[CONFIG][%s] CHECK_TRADING_TIME=%s; original execution rules retained' %
                      (self.kind, setting), flush=True)
            else:
                print('[CONFIG][%s] Original strategy has no CHECK_TRADING_TIME; time rules unchanged.' %
                      self.kind, flush=True)
        self.check_trading_time = setting
