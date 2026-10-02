# -*- coding: utf-8 -*-
"""Test GPIO OUTPUT (relay/den) cua node_agent/readers/gpio.py - task
pcm-downlink-command muc 4: `"direction": "output"`, command() on | off |
write | blink, `ms` (bat roi tu tat) / `period_ms` (chu ky nhay), cung ngu
nghia ESP32 components/gpio_out/gpio_out.c::gpio_out_execute().

2 lop test:
- MockFactory THAT cua gpiozero (pin ao) cho hanh vi: muc chan, tu tat sau
  ms, on() dung blink, invert (active-low).
- Device thay bang Mock() cho tham so blink() (on_time/off_time/n) - tranh
  phai do thoi gian that de suy ra n/period.

`Device.pin_factory` la class attribute global - fixture autouse reset ve
None sau moi test (giong tests/test_gpio_reader.py)."""
import time
from unittest.mock import Mock

import pytest
from gpiozero import Device
from gpiozero.pins.mock import MockFactory

from node_agent.readers.gpio import GpioReader


@pytest.fixture(autouse=True)
def _isolate_gpiozero_pin_factory():
    yield
    factory = Device.pin_factory
    if factory is not None:
        try:
            factory.close()
        except Exception:                                          # noqa: BLE001
            pass
    Device.pin_factory = None


def _cfg(**overrides):
    cfg = {"code": "relay1", "pin": 17, "direction": "output"}
    cfg.update(overrides)
    return cfg


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def out_reader():
    """Reader output da _connect() tren MockFactory (pin 17)."""
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(), emit=Mock())
    reader._connect()
    yield reader
    reader._close()


def _pin(n=17):
    return Device.pin_factory.pin(n)


def _mock_device_reader(**overrides):
    reader = GpioReader(_cfg(**overrides), emit=Mock())
    reader._device = Mock()
    return reader


# ----------------------------------------------------------------------
# 1) __init__ - direction

@pytest.mark.parametrize("direction", ["sideways", "out", "", "in"])
def test_init_invalid_direction_raises_value_error(direction):
    with pytest.raises(ValueError):
        GpioReader(_cfg(direction=direction), emit=Mock())


def test_init_direction_default_is_input():
    reader = GpioReader({"code": "c", "pin": 4}, emit=Mock())

    assert reader._output is False


@pytest.mark.parametrize("direction", ["output", "OUTPUT", " Output "])
def test_init_direction_output_case_and_whitespace_insensitive(direction):
    reader = GpioReader(_cfg(direction=direction), emit=Mock())

    assert reader._output is True


def test_connect_output_creates_output_device_initially_off():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(), emit=Mock())

    reader._connect()
    try:
        assert reader._device.pin is _pin()
        assert _pin().state == 0
        assert reader._read_once() == 0.0
    finally:
        reader._close()
    assert reader._device is None


# ----------------------------------------------------------------------
# 2) Kenh INPUT tu choi lenh

@pytest.mark.parametrize("cmd", ["on", "off", "write", "blink"])
def test_input_reader_rejects_every_command(cmd):
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(direction="input"), emit=Mock())
    reader._connect()
    try:
        result = reader.command(cmd, 1)
    finally:
        reader._close()

    assert result["ok"] is False
    assert "input" in result["error"]


# ----------------------------------------------------------------------
# 3) on / off / write (MockFactory that)

def test_on_then_off_drives_pin_and_read_once(out_reader):
    assert out_reader.command("on") == {"ok": True, "status": "ok"}
    assert _pin().state == 1
    assert out_reader._read_once() == 1.0

    assert out_reader.command("off") == {"ok": True, "status": "ok"}
    assert _pin().state == 0
    assert out_reader._read_once() == 0.0


@pytest.mark.parametrize("value, expected", [
    (1, 1), (0, 0), (2.5, 1), (-1, 1), (0.0, 0), (None, 0),
])
def test_write_value_nonzero_turns_on_zero_or_none_turns_off(out_reader, value, expected):
    # dat trang thai NGUOC voi ky vong truoc khi write
    if expected == 0:
        out_reader._device.on()
    else:
        out_reader._device.off()

    result = out_reader.command("write", value)

    assert result == {"ok": True, "status": "ok"}
    assert _pin().state == expected


# r2 (onoff_level): chuoi so ("1"/"0") va bool cung bi tu choi - khong doan y.
@pytest.mark.parametrize("value", ["x", "on", [1], {"a": 1}, "1", "0", True, False])
def test_write_non_numeric_value_returns_error_and_pin_unchanged(out_reader, value):
    result = out_reader.command("write", value)

    assert result["ok"] is False
    assert _pin().state == 0


@pytest.mark.parametrize("cmd", ["toggle", "identify", "zero", ""])
def test_unknown_command_returns_error(out_reader, cmd):
    result = out_reader.command(cmd)

    assert result["ok"] is False
    assert _pin().state == 0


# r2 (duration_ms): chuoi so "100", bool, NaN, {} (falsy) deu la loi.
@pytest.mark.parametrize("kw", [{"ms": "abc"}, {"period_ms": "fast"}, {"ms": [1]}, {"period_ms": {"a": 1}},
                                {"ms": "100"}, {"period_ms": "300"}, {"ms": True}, {"ms": float("nan")},
                                {"period_ms": float("nan")}, {"ms": {}}, {"ms": ""}])
def test_non_numeric_ms_or_period_returns_error_and_pin_unchanged(out_reader, kw):
    result = out_reader.command("on", **kw)

    assert result["ok"] is False
    assert _pin().state == 0


def test_command_when_device_none_returns_error():
    reader = GpioReader(_cfg(), emit=Mock())        # chua _connect

    for cmd in ("on", "off", "blink"):
        result = reader.command(cmd)
        assert result["ok"] is False
        assert "17" in result["error"]


def test_command_accepts_extra_opts_ignored(out_reader):
    assert out_reader.command("on", channel="relay1", foo="bar")["ok"] is True
    assert _pin().state == 1


# ----------------------------------------------------------------------
# 4) invert = active-low

def test_invert_true_on_drives_pin_low_but_read_once_returns_logic_on():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(invert=True), emit=Mock())
    reader._connect()
    try:
        # initial_value=False (TAT logic) -> active-low = chan muc cao
        assert _pin().state == 1
        assert reader._read_once() == 0.0

        reader.command("on")
        assert _pin().state == 0
        assert reader._read_once() == 1.0

        reader.command("off")
        assert _pin().state == 1
        assert reader._read_once() == 0.0
    finally:
        reader._close()


# ----------------------------------------------------------------------
# 5) ms - bat roi tu tat (MockFactory that, thoi gian that nho)

def test_on_with_ms_turns_on_then_auto_off(out_reader):
    result = out_reader.command("on", ms=150)

    assert result == {"ok": True, "status": "ok"}
    assert _wait_until(lambda: _pin().state == 1, timeout=0.1)
    assert _wait_until(lambda: _pin().state == 0, timeout=2.0)
    time.sleep(0.2)
    assert _pin().state == 0            # khong bat lai (n=1)


def test_write_nonzero_with_ms_also_auto_off(out_reader):
    out_reader.command("write", 1, ms=100)

    assert _wait_until(lambda: _pin().state == 1, timeout=0.1)
    assert _wait_until(lambda: _pin().state == 0, timeout=2.0)


def test_off_with_ms_just_turns_off():
    reader = _mock_device_reader()

    assert reader.command("off", ms=500)["ok"] is True
    reader._device.off.assert_called_once_with()
    reader._device.blink.assert_not_called()


def test_on_with_ms_uses_plain_on_plus_timer_not_blink():
    reader = _mock_device_reader()

    reader.command("on", ms=250)
    try:
        reader._device.on.assert_called_once_with()
        reader._device.blink.assert_not_called()
        assert reader._timer is not None
        assert reader._timer.interval == pytest.approx(0.25)
    finally:
        reader._close()


def test_off_with_ms_creates_no_timer():
    reader = _mock_device_reader()

    reader.command("off", ms=500)

    assert reader._timer is None


def test_write_zero_with_ms_creates_no_timer():
    reader = _mock_device_reader()

    reader.command("write", 0, ms=500)

    reader._device.off.assert_called_once_with()
    assert reader._timer is None


def test_on_with_ms_zero_is_plain_on():
    reader = _mock_device_reader()

    reader.command("on", ms=0)

    reader._device.on.assert_called_once_with()
    reader._device.blink.assert_not_called()


# ----------------------------------------------------------------------
# 6) blink - tham so

@pytest.mark.parametrize("kw, on_time, n", [
    ({}, 0.5, None),                                   # mac dinh 1000ms, vo han
    ({"period_ms": 400}, 0.2, None),
    ({"period_ms": 50}, 0.05, None),                   # <100 -> kep 100ms
    ({"period_ms": 100}, 0.05, None),
    ({"period_ms": 200, "ms": 1000}, 0.1, None),       # r2: ms -> timer, blink luon n=None
    ({"ms": 3000}, 0.5, None),
    ({"period_ms": 1000, "ms": 100}, 0.5, None),
    ({"period_ms": 300.9}, 0.15, None),                # float cat ve int
    ({"period_ms": 0}, 0.5, None),                     # <=0 -> 1000
    ({"period_ms": -50}, 0.5, None),                   # r2 regression: am -> 1000
    ({"period_ms": 1e13}, (2 ** 31 - 1) / 2000.0, None),   # kep INT32_MAX
])
def test_blink_params(kw, on_time, n):
    reader = _mock_device_reader()
    device = reader._device

    try:
        result = reader.command("blink", **kw)
    finally:
        reader._close()             # huy timer (neu co ms)

    assert result == {"ok": True, "status": "ok"}
    device.blink.assert_called_once()
    call = device.blink.call_args.kwargs
    assert call["on_time"] == pytest.approx(on_time)
    assert call["off_time"] == pytest.approx(on_time)
    assert call["n"] == n
    assert call["background"] is True


def test_blink_real_pin_toggles(out_reader):
    out_reader.command("blink", period_ms=100)

    assert _wait_until(lambda: _pin().state == 1, timeout=1.0)
    assert _wait_until(lambda: _pin().state == 0, timeout=1.0)
    assert _wait_until(lambda: _pin().state == 1, timeout=1.0)


def test_on_stops_running_blink(out_reader):
    out_reader.command("blink", period_ms=100)
    assert _wait_until(lambda: _pin().state == 1, timeout=1.0)

    out_reader.command("on")

    time.sleep(0.3)                     # > 2 chu ky nhay
    assert _pin().state == 1


def test_off_stops_running_blink(out_reader):
    out_reader.command("blink", period_ms=100)

    out_reader.command("off")

    time.sleep(0.3)
    assert _pin().state == 0


def test_blink_with_ms_turns_off_exactly_after_ms_mid_cycle(out_reader):
    """r2 regression: blink ms=300, period=1000 (on 500ms) -> timer tat dung
    ~300ms GIUA nua chu ky bat, va KHONG bat lai o moc 1000ms."""
    t0 = time.monotonic()
    out_reader.command("blink", period_ms=1000, ms=300)

    assert _wait_until(lambda: _pin().state == 1, timeout=0.1)
    assert _wait_until(lambda: _pin().state == 0, timeout=1.0)
    elapsed = time.monotonic() - t0
    # Chi can can duoi: assert "van TAT sau moc 1000ms" ben duoi da phan biet
    # timer voi blink tu nhien; can tren chat de flaky tren CI (reviewer r3).
    assert elapsed >= 0.25
    time.sleep(0.9)                         # qua moc 1000ms blink se bat lai
    assert _pin().state == 0


# ----------------------------------------------------------------------
# 7) r2 regression - timer / _gen

def test_on_ms_then_off_before_expiry_timer_does_not_act(out_reader, monkeypatch):
    out_reader.command("on", ms=150)
    out_reader.command("off")
    assert out_reader._timer is None
    out_reader.command("write", 1)            # bat lai KHONG kem ms
    time.sleep(0.35)                          # qua moc 150ms cua timer cu

    assert _pin().state == 1                  # timer cu khong tat


def test_on_ms_then_new_on_ms_only_new_timer_effective(out_reader):
    out_reader.command("on", ms=150)
    t1 = time.monotonic()
    time.sleep(0.05)
    out_reader.command("on", ms=500)

    time.sleep(0.25)                          # qua moc 150ms cua timer cu
    assert _pin().state == 1
    assert _wait_until(lambda: _pin().state == 0, timeout=1.0)
    assert time.monotonic() - t1 >= 0.5


def test_stale_expire_call_with_old_gen_is_ignored(out_reader):
    out_reader.command("on", ms=10000)
    old_gen, dev = out_reader._gen, out_reader._device
    out_reader.command("on")

    out_reader._expire(old_gen, dev)

    assert _pin().state == 1


def test_expire_after_device_replaced_is_ignored(out_reader):
    out_reader.command("on", ms=10000)
    gen, dev = out_reader._gen, out_reader._device
    other = Mock()
    out_reader._device = other

    out_reader._expire(gen, dev)

    other.off.assert_not_called()
    out_reader._device = dev
    assert _pin().state == 1


def test_timer_firing_after_close_does_not_raise():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(), emit=Mock())
    reader._connect()
    reader.command("on", ms=10000)
    gen, dev = reader._gen, reader._device

    reader._close()
    assert reader._timer is None
    reader._expire(gen, dev)                 # nhu timer da lo kich hoat truoc cancel

    assert reader._device is None


def test_blink_with_ms_timer_interval_exact_and_expire_turns_off():
    """Khang dinh dung 300ms khong phu thuoc thoi gian that (reviewer r3)."""
    reader = _mock_device_reader()
    device = reader._device
    try:
        reader.command("blink", period_ms=1000, ms=300)
        assert reader._timer.interval == pytest.approx(0.3)

        reader._expire(reader._gen, device)

        device.off.assert_called_once_with()
        assert reader._timer is None
    finally:
        reader._close()


def test_close_cancels_pending_timer_immediately(out_reader):
    """reviewer r3: test cu (ms=100 + sleep) van xanh khi bo dong cancel o
    _close - timer hen xa (10s) nen finished CHI set duoc khi bi cancel."""
    out_reader.command("on", ms=10000)
    old_timer = out_reader._timer

    out_reader._close()

    assert old_timer.finished.is_set()
    old_timer.join(timeout=0.5)
    assert not old_timer.is_alive()
    assert out_reader._timer is None


def test_close_cancels_pending_timer_no_thread_error(out_reader):
    errors = []
    import threading
    old_hook = threading.excepthook
    threading.excepthook = lambda a: errors.append(a)
    try:
        out_reader.command("on", ms=100)
        old_timer = out_reader._timer
        out_reader._close()
        # cancel() dat finished NGAY - khong co dong cancel trong _close thi
        # finished chi set sau 100ms (guard identity trong _expire che mat).
        assert old_timer.finished.is_set()
        time.sleep(0.25)
    finally:
        threading.excepthook = old_hook

    assert errors == []


@pytest.mark.parametrize("ms", [1e13, float("inf"), 2 ** 40])
def test_huge_ms_clamped_no_overflow_ok_true(out_reader, ms):
    result = out_reader.command("on", ms=ms)

    assert result == {"ok": True, "status": "ok"}
    assert out_reader._timer.interval == pytest.approx((2 ** 31 - 1) / 1000.0)
    assert _pin().state == 1


@pytest.mark.parametrize("cmd", ["on", "blink"])
def test_nan_ms_rejected(out_reader, cmd):
    result = out_reader.command(cmd, ms=float("nan"))

    assert result["ok"] is False
    assert _pin().state == 0


def test_huge_period_blink_clamped_ok_true(out_reader):
    result = out_reader.command("blink", period_ms=float("inf"))

    assert result == {"ok": True, "status": "ok"}


# ----------------------------------------------------------------------
# 8) r3 regression - ghi chan raise thi KHONG dung toi timer/_gen cu
#    (reviewer: neu huy timer TRUOC khi ghi, ghi loi -> relay ket BAT mai)

@pytest.mark.parametrize("cmd, method", [("on", "on"), ("off", "off"), ("blink", "blink"), ("write", "on")])
def test_write_failure_keeps_old_timer_and_gen(cmd, method):
    reader = _mock_device_reader()
    device = reader._device
    try:
        assert reader.command("on", ms=200) == {"ok": True, "status": "ok"}
        old_timer, old_gen = reader._timer, reader._gen
        # lan goi DAU raise; lan sau (vd timer cu goi off()) thanh cong
        getattr(device, method).side_effect = [RuntimeError("gpio boom"), None]
        off_calls_before = device.off.call_count

        with pytest.raises(RuntimeError, match="gpio boom"):
            reader.command(cmd, 1 if cmd == "write" else None, ms=5000)

        assert reader._timer is old_timer
        assert reader._gen == old_gen
        assert old_timer.is_alive()
        # timer CU van tat chan dung han (~200ms)
        assert _wait_until(lambda: device.off.call_count > off_calls_before + (1 if method == "off" else 0),
                           timeout=1.0)
        assert reader._timer is None
    finally:
        reader._close()


def test_write_failure_real_pin_old_timer_still_turns_off(out_reader, monkeypatch):
    out_reader.command("on", ms=200)
    dev = out_reader._device
    monkeypatch.setattr(type(dev), "blink", Mock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        out_reader.command("blink", ms=10000)

    assert _pin().state == 1
    assert _wait_until(lambda: _pin().state == 0, timeout=1.0)


def test_successful_write_cancels_old_timer_and_bumps_gen():
    reader = _mock_device_reader()
    device = reader._device
    try:
        reader.command("on", ms=200)
        old_timer, old_gen = reader._timer, reader._gen

        assert reader.command("on") == {"ok": True, "status": "ok"}

        assert reader._gen == old_gen + 1
        assert reader._timer is None
        old_timer.join(timeout=0.5)
        assert not old_timer.is_alive()
        time.sleep(0.3)
        device.off.assert_not_called()          # timer cu da bi huy
    finally:
        reader._close()
