# -*- coding: utf-8 -*-
"""Test node_agent/readers/sim.py (SimReader) - reader gia lap (khong can
thiet bi that), dung "mode": "sim" trong channels.json.

Truoc day SimReader KHONG co __init__ rieng (doc thang self.cfg.get(...)
trong _run() moi vong lap, khong ep kieu, ngoai try/except) - cung lop bug
da fix o readers/gpio.py va readers/modbus.py (Phase 3, python-reviewer
2026-09-25): channels.json sua tay ghi poll_ms sai kieu (vd chuoi khong
parse duoc) se lam thread _run() chet im lang GIUA vong lap (khong phai
ngay luc khoi tao), status.online van la True truoc do -> health-check bao
SAI la kenh van online. Da them __init__() ep kieu NGAY (duoc agent.py::
_build_readers() boc try/except) - file nay la regression test cho fix do,
dung pattern thread+join(timeout=...) giong het test_gpio_reader.py/
test_modbus_reader.py de tranh treo suite neu regression tai xuat."""
import threading
import time
from unittest.mock import Mock

import pytest

from node_agent.readers.sim import SimReader


def _cfg(**overrides):
    cfg = {"code": "ch1"}
    cfg.update(overrides)
    return cfg


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ----------------------------------------------------------------------
# 1) SimReader.__init__() - ep kieu + default

def test_init_defaults_center_50_spread_1_poll_1s():
    reader = SimReader(_cfg(), emit=Mock())

    assert reader._center == 50.0
    assert reader._spread == 1.0
    assert reader._poll_s == 1.0


def test_init_reads_center_and_spread_from_cfg():
    reader = SimReader(_cfg(center=412.4, spread=0.3), emit=Mock())

    assert reader._center == 412.4
    assert reader._spread == 0.3


def test_init_poll_ms_numeric_string_parses_correctly():
    """Khong phai case fail: poll_ms dang chuoi so hop le ("500") van phai
    parse duoc binh thuong, KHONG raise."""
    reader = SimReader(_cfg(poll_ms="500"), emit=Mock())

    assert reader._poll_s == 0.5


def test_init_poll_ms_non_numeric_string_raises_value_error():
    """Regression: channels.json sua tay ghi poll_ms khong parse duoc (vd
    "abc") PHAI raise ValueError NGAY trong __init__ - KHONG duoc de lot
    vao _run() roi thread chet im lang giua vong lap."""
    with pytest.raises(ValueError):
        SimReader(_cfg(poll_ms="abc"), emit=Mock())


def test_init_center_non_numeric_string_raises_value_error():
    """Cung mach ep kieu ap dung cho center/spread, khong rieng poll_ms."""
    with pytest.raises(ValueError):
        SimReader(_cfg(center="abc"), emit=Mock())


def test_init_poll_ms_below_100ms_is_clamped_to_0_1s():
    reader = SimReader(_cfg(poll_ms=10), emit=Mock())

    assert reader._poll_s == 0.1


# ----------------------------------------------------------------------
# 2) SimReader._run() - chay that trong thread rieng

def test_run_sets_online_and_emits_value_within_center_spread_range():
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, raw, quality, ok))

    reader = SimReader(_cfg(center=100.0, spread=2.0, poll_ms=10), emit=fake_emit)
    t = threading.Thread(target=reader._run, daemon=True)
    t.start()

    assert _wait_until(lambda: reader.status.online), "reader khong online"
    assert _wait_until(lambda: len(emitted) >= 1), "khong emit gia tri nao"

    reader._stop.set()
    t.join(timeout=2)

    assert not t.is_alive()
    code, value, raw, quality, ok = emitted[0]
    assert code == "ch1"
    assert raw is None
    assert quality == 0
    assert ok is True
    # walk = center + spread*sin(...) + uniform(-spread*0.2, spread*0.2)
    # -> bien do toi da ly thuyet la center +/- spread*1.2
    assert 100.0 - 2.0 * 1.2 <= value <= 100.0 + 2.0 * 1.2


def test_run_stops_cleanly_when_stop_event_set():
    reader = SimReader(_cfg(poll_ms=10), emit=Mock())
    t = threading.Thread(target=reader._run, daemon=True)
    t.start()

    assert _wait_until(lambda: reader.status.online), "reader khong online"

    reader._stop.set()
    t.join(timeout=2)

    assert not t.is_alive(), "reader._run() van con chay sau khi _stop.set()"


# ----------------------------------------------------------------------
# 3) SimReader.command() - default tra ok bat ke cmd/value

def test_command_returns_ok_regardless_of_cmd_and_value():
    reader = SimReader(_cfg(), emit=Mock())

    assert reader.command("zero") == {"ok": True, "status": "ok"}
    assert reader.command("tare", value=123) == {"ok": True, "status": "ok"}
    assert reader.command("bat_ky_gi_khac", value=None) == {"ok": True, "status": "ok"}
