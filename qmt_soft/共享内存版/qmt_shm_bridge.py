# coding: utf-8
"""Standalone QMT market-data bridge. Compatible with Python 3.6.

Import this ONE file as a QMT Python strategy. It uses named, anonymous Windows
memory mappings, not disk files or sockets. There are no trading endpoints.
The external Python process may also import the transport classes in this file.
"""
from __future__ import print_function

import json
import math
import mmap
import re
import struct
import threading
import time
import uuid
import zlib

CHANNEL = "panzhong_v1"
# True: PZ scans only in trading sessions. False: PZ keeps scanning after hours.
# This setting is sent to the external scanner, not used to stop bridge traffic.
CHECK_TRADING_TIME = True
REFERENCE_DATE = None  # None: original setting; "": realtime; "YYYYMMDD": history.
PANZHONG_ENABLED = True
THS_ENABLED = True
PANZHONG_HISTORY_RELOAD_TIME = "09:15:00"
THS_HISTORY_RELOAD_TIME = "09:25:00"

# None keeps the corresponding value in the original source. No credentials here.
PANZHONG_OPTIONS = {
    "MIN_REALTIME_GAIN_PCT": None,
    "HIGH_GAIN_THRESHOLD_PCT": None,
    "MAX_YIN_COUNT_20": None,
    "MIN_HIGH_GAIN_COUNT_10D": None,
    "MIN_LOW_ABOVE_MA5_COUNT_10D": None,
    "PZ1_MAX_YIN_COUNT_20": None,
    "HISTORY_REQUEST_DAYS": None,
    "KLINE_TOTAL_DAYS": None,
    "HISTORY_BATCH_SIZE": None,
    "TIMER_INTERVAL_TEXT": None,
    "REDIS_TTL_SEC": None,
    "DINGTALK_ENABLED": None,
    "DINGTALK_RETRY_COUNT": None,
    "DINGTALK_PERSIST_RETRY_COUNT": None,
    "DINGTALK_PERSIST_RETRY_MAX_DELAY_SEC": None,
    "DINGTALK_MAX_YIN_COUNT_20": None,
    "LOG_ENABLED": None,
    "LOG_SELECTED_LIMIT": None,
    "LOG_BASE_DIR": None,
    "STRATEGY_DIR_NAME": None,
}
THS_OPTIONS = {
    "REDIS_QMT_KLINE_DAYS": None,
    "REDIS_QMT_KLINE_TTL_SEC": None,
    "REDIS_STATUS_TTL_SEC": None,
    "HISTORY_BATCH_SIZE": None,
    "HISTORY_REQUEST_DAYS": None,
    "PUBLISH_INTERVAL_TEXT": None,
    "CLOSE_BAR_REFRESH_SEC": None,
    "LOG_BASE_DIR": None,
    "STRATEGY_DIR_NAME": None,
}

PROTOCOL = 1
MAGIC = b"QMTSHM01"
HEADER = struct.Struct("<8sQII")
HEADER_SIZE = HEADER.size
MAX_DECODED = 64 * 1024 * 1024
STALE_AFTER = 15.0
POLL_BUDGET = 0.25


class BridgeError(RuntimeError):
    pass


class BridgeCancelled(KeyboardInterrupt):
    """External launcher cancellation; bypasses strategy API fallback handlers."""
    pass


class FrameTooLarge(BridgeError):
    pass


class BridgeTimeout(BridgeError):
    pass


class BridgeRestarted(BridgeError):
    pass


class RemoteError(BridgeError):
    pass


def strategy_config():
    if not isinstance(CHECK_TRADING_TIME, bool):
        raise BridgeError("CHECK_TRADING_TIME must be True or False")
    if not isinstance(PANZHONG_ENABLED, bool) or not isinstance(THS_ENABLED, bool):
        raise BridgeError("Strategy enabled switches must be True or False")
    if REFERENCE_DATE is not None and (not isinstance(REFERENCE_DATE, str) or
            REFERENCE_DATE and not re.match(r"^\d{8}$", REFERENCE_DATE)):
        raise BridgeError("REFERENCE_DATE must be None, empty, or YYYYMMDD")
    for clock in (PANZHONG_HISTORY_RELOAD_TIME, THS_HISTORY_RELOAD_TIME):
        if not isinstance(clock, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$", clock):
            raise BridgeError("History reload time must be HH:MM:SS")
    if not isinstance(PANZHONG_OPTIONS, dict) or not isinstance(THS_OPTIONS, dict):
        raise BridgeError("Strategy options must be dictionaries")
    return {"version": 2, "CHECK_TRADING_TIME": CHECK_TRADING_TIME,
            "REFERENCE_DATE": REFERENCE_DATE,
            "enabled": {"panzhong": PANZHONG_ENABLED, "ths": THS_ENABLED},
            "history_reload_times": {"panzhong": PANZHONG_HISTORY_RELOAD_TIME,
                                     "ths": THS_HISTORY_RELOAD_TIME},
            "options": {"panzhong": dict(PANZHONG_OPTIONS), "ths": dict(THS_OPTIONS)}}


class SharedSlot(object):
    """One writer per slot; a seqlock and CRC reject incomplete frames."""

    def __init__(self, name, capacity):
        self.capacity = int(capacity)
        self.mapping = mmap.mmap(-1, HEADER_SIZE + self.capacity, tagname=name)

    def write(self, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_DECODED:
            raise FrameTooLarge("JSON message exceeds decoded size limit")
        payload = zlib.compress(raw, 1)
        if len(payload) > self.capacity:
            raise FrameTooLarge("Shared-memory frame exceeds slot capacity")
        old = HEADER.unpack(self.mapping[:HEADER_SIZE])
        seq = old[1] if old[0] == MAGIC else 0
        seq = (seq + 2) & ~1
        checksum = zlib.crc32(payload) & 0xffffffff
        self.mapping[:HEADER_SIZE] = HEADER.pack(MAGIC, seq - 1, len(payload), checksum)
        self.mapping[HEADER_SIZE:HEADER_SIZE + len(payload)] = payload
        self.mapping[:HEADER_SIZE] = HEADER.pack(MAGIC, seq, len(payload), checksum)

    def read(self):
        first = self.mapping[:HEADER_SIZE]
        magic, sequence, length, checksum = HEADER.unpack(first)
        if magic != MAGIC or sequence & 1 or not 0 < length <= self.capacity:
            return None
        payload = self.mapping[HEADER_SIZE:HEADER_SIZE + length]
        if first != self.mapping[:HEADER_SIZE]:
            return None
        if zlib.crc32(payload) & 0xffffffff != checksum:
            return None
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(payload, MAX_DECODED + 1)
            if len(raw) > MAX_DECODED or not decoder.eof or decoder.unused_data:
                return None
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, zlib.error):
            return None

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def _channel_name(channel):
    if not isinstance(channel, str) or not re.match(r"^[A-Za-z0-9_]{1,64}$", channel):
        raise BridgeError("Channel must contain 1-64 ASCII letters, digits or underscores")
    return "Local\\QmtPzShm_" + channel


def _open_slots(owner, channel):
    prefix = _channel_name(channel)
    owner.request = owner.response = owner.status = None
    try:
        owner.request = SharedSlot(prefix + "_request", 1024 * 1024)
        owner.response = SharedSlot(prefix + "_response", 32 * 1024 * 1024)
        owner.status = SharedSlot(prefix + "_status", 4096)
    except Exception:
        _close_slots(owner)
        raise


def _close_slots(owner):
    for name in ("request", "response", "status"):
        slot = getattr(owner, name, None)
        if slot is not None:
            slot.close()


def to_wire(value):
    """JSON-only serialization, including old pandas/numpy QMT return values."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "columns") and hasattr(value, "index") and hasattr(value, "values"):
        return {"__qmt_frame__": 1,
                "columns": [str(x) for x in value.columns],
                "index": [x.strftime("%Y%m%d") if hasattr(x, "strftime") else str(x)
                          for x in value.index],
                "data": [[to_wire(x) for x in row] for row in value.values]}
    if isinstance(value, dict):
        return {str(key): to_wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_wire(item) for item in value]
    if hasattr(value, "tolist"):
        return to_wire(value.tolist())
    if hasattr(value, "item"):
        return to_wire(value.item())
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d%H%M%S")
    raise BridgeError("Unsupported market data type: " + type(value).__name__)


def _stocks(value, maximum=10000, markets=False):
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise BridgeError("Invalid stock list length")
    for code in value:
        if not isinstance(code, str) or not (
                re.match(r"^[0-9A-Za-z_-]{1,24}\.(SH|SZ|BJ|SF|DF|ZF|IF|GF)$", code)
                or markets and code in ("SH", "SZ", "BJ")):
            raise BridgeError("Invalid instrument code")
    return value


def dispatch_market(context, method, args, kwargs):
    """Explicit read-only API allowlist. No arbitrary method lookup."""
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise BridgeError("Invalid request arguments")
    if method == "ping" and not args and not kwargs:
        return {"protocol": PROTOCOL, "channel_type": "anonymous_named_memory",
                "strategy_config": strategy_config()}
    if method == "get_stock_list_in_sector" and len(args) == 1 and not kwargs:
        sector = args[0]
        if not isinstance(sector, str) or not 0 < len(sector) <= 128:
            raise BridgeError("Invalid sector")
        return context.get_stock_list_in_sector(sector)
    if method == "subscribe_whole_quote" and len(args) == 1 and not kwargs:
        markets = args[0]
        if not isinstance(markets, list) or not markets or any(
                x not in ("SH", "SZ", "BJ") for x in markets):
            raise BridgeError("Invalid market subscription")
        context.subscribe_whole_quote(markets)
        return True
    if method == "get_full_tick" and len(args) == 1 and not kwargs:
        return context.get_full_tick(_stocks(args[0], markets=True))
    if method == "get_metadata" and len(args) == 1 and not kwargs:
        codes = _stocks(args[0], maximum=512)
        get_detail = getattr(context, "get_instrument_detail", None)
        if not callable(get_detail):
            get_detail = getattr(context, "get_instrumentdetail", None)
        result = {}
        for code in codes:
            name, detail = None, None
            try:
                name = context.get_stock_name(code)
            except Exception:
                pass
            try:
                detail = get_detail(code) if callable(get_detail) else None
            except Exception:
                pass
            result[code] = {"name": name, "detail": detail}
        return result
    if method == "get_market_data_ex" and len(args) == 2:
        fields, codes = args
        _stocks(codes, maximum=512)
        if not isinstance(fields, list) or not fields or any(
                f not in ("open", "high", "low", "close", "preClose", "volume", "amount")
                for f in fields):
            raise BridgeError("Invalid history fields")
        if set(kwargs) - set(("period", "end_time", "count", "dividend_type", "fill_data")):
            raise BridgeError("Unsupported history option")
        count = kwargs.get("count", 65)
        end_time = kwargs.get("end_time", "")
        if (kwargs.get("period", "1d") != "1d"
                or not isinstance(count, int) or not 1 <= count <= 2048
                or kwargs.get("dividend_type", "front") not in ("front", "none")
                or not isinstance(end_time, str)
                or end_time and not re.match(r"^\d{8}(\d{6})?$", end_time)
                or "fill_data" in kwargs and not isinstance(kwargs["fill_data"], bool)):
            raise BridgeError("History query is outside bridge limits")
        data = context.get_market_data_ex(fields, codes, **kwargs)
        result = {}
        for code in codes:
            if isinstance(data, dict):
                result[code] = data.get(code)
            elif hasattr(data, "minor_xs"):
                result[code] = data.minor_xs(code)
            else:
                try:
                    result[code] = data[code]
                except (KeyError, TypeError):
                    result[code] = None
        return result
    raise BridgeError("Method is not allowed: " + str(method))


class BridgeServer(object):
    def __init__(self, context, channel=CHANNEL):
        strategy_config()
        self.context = context
        self.channel = channel
        self.server_id = uuid.uuid4().hex
        self.started = time.time()
        self.last_request_id = None
        self.last_status = 0.0
        self.closed = False
        _open_slots(self, channel)
        old = self.status.read()
        if (isinstance(old, dict) and old.get("state") in ("ready", "busy")
                and 0 <= time.time() - old.get("heartbeat", 0) < STALE_AFTER):
            _close_slots(self)
            raise BridgeError("Another QMT bridge is active on this channel")
        self._heartbeat("ready", force=True)

    def _heartbeat(self, state="ready", force=False):
        now = time.time()
        if force or now - self.last_status >= .5:
            self.status.write({"protocol": PROTOCOL, "server_id": self.server_id,
                               "state": state, "heartbeat": now, "started": self.started,
                               "last_request_id": self.last_request_id,
                               "strategy_config": strategy_config()})
            self.last_status = now

    def serve_once(self):
        if self.closed:
            return False
        self._heartbeat()
        request = self.request.read()
        if not isinstance(request, dict):
            return False
        request_id = request.get("id")
        if not isinstance(request_id, str) or request_id == self.last_request_id:
            return False
        self.last_request_id = request_id
        deadline = request.get("deadline")
        if (request.get("protocol") != PROTOCOL
                or request.get("server_id") != self.server_id
                or not isinstance(deadline, (int, float)) or deadline < time.time()):
            return True
        self._heartbeat("busy", force=True)
        response = {"id": request_id, "server_id": self.server_id, "protocol": PROTOCOL}
        try:
            value = dispatch_market(self.context, request.get("method"),
                                    request.get("args", []), request.get("kwargs", {}))
            response.update({"ok": True, "data": to_wire(value), "created": time.time()})
            self.response.write(response)
        except Exception as error:
            response.pop("data", None)
            response.update({"ok": False, "error": type(error).__name__ + ": " + str(error)[:300],
                             "created": time.time()})
            self.response.write(response)
        finally:
            self._heartbeat("ready", force=True)
        return True

    def close(self):
        if not self.closed:
            self._heartbeat("stopped", force=True)
            self.closed = True
            _close_slots(self)


class RpcClient(object):
    def __init__(self, channel=CHANNEL, timeout=120, stop_event=None):
        self.timeout = float(timeout)
        self.stop_event = stop_event
        self.server_id = None
        self.lock = threading.Lock()
        _open_slots(self, channel)

    def wait_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            status = self.status.read()
            if (isinstance(status, dict) and status.get("protocol") == PROTOCOL
                    and status.get("state") in ("ready", "busy")
                    and 0 <= time.time() - status.get("heartbeat", 0) < STALE_AFTER):
                if self.server_id and self.server_id != status.get("server_id"):
                    raise BridgeRestarted("QMT bridge restarted; restart the external program")
                self.server_id = status["server_id"]
                return status
            time.sleep(.02)
        raise BridgeTimeout("No live QMT shared-memory bridge. Run qmt_shm_bridge in QMT first")

    def _check_cancelled(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise BridgeCancelled("External program stop requested")

    def assert_online(self):
        self._check_cancelled()
        status = self.status.read()
        # A seqlock reader intentionally returns None while another process is
        # publishing a frame. That transient result is not a lost QMT bridge.
        retry_until = time.monotonic() + .25
        while status is None and time.monotonic() < retry_until:
            time.sleep(.002)
            status = self.status.read()
        if not isinstance(status, dict) or status.get("protocol") != PROTOCOL:
            raise BridgeError("QMT bridge status is missing")
        if status.get("server_id") != self.server_id:
            raise BridgeRestarted("QMT bridge restarted; restart the external program")
        if status.get("state") not in ("ready", "busy"):
            raise BridgeError("QMT bridge has stopped")
        if not 0 <= time.time() - status.get("heartbeat", 0) < STALE_AFTER:
            # Historical QMT API calls can legitimately block for >15 seconds.
            # A second strategy must not mistake that call for a dead bridge.
            request = self.request.read() if status.get("state") == "busy" else None
            if not (isinstance(request, dict)
                    and request.get("server_id") == self.server_id
                    and request.get("id") == status.get("last_request_id")
                    and isinstance(request.get("deadline"), (int, float))
                    and time.time() < request["deadline"]):
                raise BridgeTimeout("QMT bridge heartbeat expired")
        return status

    def call(self, method, args=None, kwargs=None):
        with self.lock:
            self.assert_online()
            request_id = uuid.uuid4().hex
            deadline = time.monotonic() + self.timeout
            previous_header = self.response.mapping[:HEADER_SIZE]
            self.request.write({"protocol": PROTOCOL, "server_id": self.server_id,
                                "id": request_id, "deadline": time.time() + self.timeout,
                                "method": method, "args": args or [], "kwargs": kwargs or {}})
            while time.monotonic() < deadline:
                self._check_cancelled()
                # Full-market responses can be large. Do not decompress the old
                # frame every 5 ms while the QMT timer handles the next request.
                header = self.response.mapping[:HEADER_SIZE]
                response = None
                if header != previous_header:
                    response = self.response.read()
                    previous_header = header
                if isinstance(response, dict) and response.get("id") == request_id:
                    self.assert_online()
                    if response.get("server_id") != self.server_id:
                        raise BridgeRestarted("Response belongs to another QMT bridge instance")
                    if response.get("ok"):
                        return response.get("data")
                    raise RemoteError(response.get("error", "QMT market-data request failed"))
                status = self.status.read()
                if isinstance(status, dict) and status.get("server_id") != self.server_id:
                    raise BridgeRestarted("QMT bridge restarted during a request")
                if isinstance(status, dict) and status.get("state") == "stopped":
                    raise BridgeError("QMT bridge stopped during a request")
                time.sleep(.005)
            raise BridgeTimeout("QMT request timed out: " + method)

    def close(self):
        _close_slots(self)


_qmt_bridge = None
_qmt_polling = False


def init(ContextInfo):
    global _qmt_bridge
    if _qmt_bridge is not None:
        return
    try:
        server = BridgeServer(ContextInfo, CHANNEL)
        try:
            ContextInfo.run_time("qmt_shm_poll", "1nSecond", "2020-01-01 00:00:00", "SH")
        except Exception:
            server.close()
            raise
        _qmt_bridge = server
        print("[SHM READY] channel=%s; start external check program. No Redis/HTTP in QMT." % CHANNEL)
        print("[SHM CONFIG] CHECK_TRADING_TIME=%s; external PZ scanner follows this setting." %
              CHECK_TRADING_TIME)
        print("[SHM] This is the market-data bridge; do not run the original scanner at the same time.")
    except Exception as error:
        print("[SHM FAILED] %s: %s" % (type(error).__name__, error))
        print("[SHM] If mmap is blocked by this QMT build, this method is unavailable.")
        raise


def qmt_shm_poll(ContextInfo):
    global _qmt_polling
    if _qmt_bridge is None or _qmt_polling:
        return
    _qmt_polling = True
    try:
        deadline = time.monotonic() + POLL_BUDGET
        idle_until = time.monotonic() + .015
        while time.monotonic() < deadline:
            if _qmt_bridge.serve_once():
                idle_until = time.monotonic() + .015
            elif time.monotonic() >= idle_until:
                break
            else:
                time.sleep(.002)
    finally:
        _qmt_polling = False


def handlebar(ContextInfo):
    qmt_shm_poll(ContextInfo)


def stop(ContextInfo):
    global _qmt_bridge
    if _qmt_bridge is not None:
        _qmt_bridge.close()
        _qmt_bridge = None
    print("[SHM STOPPED]")
