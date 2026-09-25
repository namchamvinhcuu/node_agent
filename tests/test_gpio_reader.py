# -*- coding: utf-8 -*-
"""Test node_agent/readers/gpio.py (GpioReader) - Phase 3/3 multi-protocol
readers, digital input qua `gpiozero`.

Khac Modbus/MQTT: gpiozero co san `gpiozero.pins.mock.MockFactory` cho phep
test CHAY THAT logic cua thu vien (drive_high()/drive_low() tren 1 "chan ao")
thay vi mock thuan Python (patch DigitalInputDevice) - da verify thuc nghiem
tren may dev nay (x86, khong co Pi that): MockFactory hoat dong dung nhu tai
lieu, KHONG can bien moi truong dac biet. Vi vay phan lon test o day dung
MockFactory THAT, chi mock thuan Python (bo Device.pin_factory ve None) cho
truong hop "khong co pin factory that" (BadPinFactory - mo phong dung 1 Pi
thieu thu vien nen tang lgpio/RPi.GPIO/pigpio, hoac chay tren PC khong phai
Pi).

`Device.pin_factory` la CLASS ATTRIBUTE (global state cua gpiozero) - fixture
`_isolate_gpiozero_pin_factory` duoi day RESET no ve None sau MOI test (dong
factory truoc khi reset de giai phong pin mock), tranh 1 test set MockFactory
roi de lai pin/device song sang test ke tiep."""
import threading
import time
from unittest.mock import Mock

import pytest
from gpiozero import Device
from gpiozero.pins.mock import MockFactory

from node_agent.readers.gpio import GpioReader, _cfg_bool


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
    cfg = {"code": "ch1", "pin": 4}
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
# 1) GpioReader.__init__() - doc cfg + default

def test_init_raises_keyerror_when_pin_missing():
    with pytest.raises(KeyError):
        GpioReader({"code": "ch1"}, emit=Mock())


def test_init_defaults_pull_up_invert_false_bounce_none():
    reader = GpioReader(_cfg(), emit=Mock())

    assert reader._pull_up is False
    assert reader._invert is False
    assert reader._bounce_s is None


def test_init_pull_up_and_invert_true_are_read_from_cfg():
    reader = GpioReader(_cfg(pull_up=True, invert=True), emit=Mock())

    assert reader._pull_up is True
    assert reader._invert is True


def test_init_bounce_ms_positive_converts_to_seconds():
    reader = GpioReader(_cfg(bounce_ms=250), emit=Mock())

    assert reader._bounce_s == 0.25


def test_init_bounce_ms_zero_gives_none():
    reader = GpioReader(_cfg(bounce_ms=0), emit=Mock())

    assert reader._bounce_s is None


# ----------------------------------------------------------------------
# 1b) Regression finding python-reviewer 2026-09-25 (Phase 3/GPIO) - main
#     session da tu sua 2 finding nay, day la regression test cho CHINH 2
#     finding do (khong phai test-writer sua code).

def test_init_poll_ms_as_numeric_string_parses_correctly():
    """Major - khong phai case fail: channels.json sua tay ghi so dang
    chuoi ("poll_ms": "200") van phai parse duoc binh thuong, KHONG raise."""
    reader = GpioReader(_cfg(poll_ms="200"), emit=Mock())

    assert reader._poll_s == 0.2


def test_init_poll_ms_non_numeric_string_raises_value_error():
    """Major - regression: `channels.json` sua tay ghi `poll_ms` khong parse
    duoc (vd "abc") PHAI raise ngay trong __init__ (duoc agent.py::
    _build_readers() bat va skip kenh) - KHONG duoc de lot vao _run() roi
    TypeError khong bat giua vong lap tren thread rieng (thread chet im
    lang vinh vien, status.online van con True tu truoc do vi da set truoc
    khi crash -> health-check bao SAI la kenh van online)."""
    with pytest.raises(ValueError):
        GpioReader(_cfg(poll_ms="abc"), emit=Mock())


def test_cfg_bool_string_false_is_false_not_truthy_nonempty_string():
    """Minor - regression: bool("false") == True (chuoi khong rong) la bug
    cu - _cfg_bool() phai so sanh tuong minh, "false" (chuoi, hand-edit JSON
    sai) phai tra ve False, khong duoc am tham dao nguoc y dinh."""
    assert _cfg_bool({"pull_up": "false"}, "pull_up", False) is False


def test_cfg_bool_string_true_and_one_are_true():
    assert _cfg_bool({"pull_up": "true"}, "pull_up", False) is True
    assert _cfg_bool({"pull_up": "1"}, "pull_up", False) is True


def test_cfg_bool_real_json_boolean_still_works():
    assert _cfg_bool({"pull_up": True}, "pull_up", False) is True
    assert _cfg_bool({"pull_up": False}, "pull_up", False) is False


def test_cfg_bool_missing_key_returns_default():
    assert _cfg_bool({}, "pull_up", False) is False
    assert _cfg_bool({}, "invert", True) is True


def test_init_pull_up_string_false_regression_gives_false():
    """Regression o muc GpioReader.__init__() (khong chi ham _cfg_bool don
    le) - dam bao __init__ THAT SU dung _cfg_bool() cho ca pull_up lan
    invert, khong con `bool(cfg.get(...))` cu."""
    reader = GpioReader(_cfg(pull_up="false", invert="false"), emit=Mock())

    assert reader._pull_up is False
    assert reader._invert is False


# ----------------------------------------------------------------------
# 2) GpioReader._connect() - dung MockFactory THAT

def test_connect_creates_device_bound_to_configured_pin():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(pin=17), emit=Mock())

    reader._connect()

    assert reader._device is not None
    assert reader._device.pin is Device.pin_factory.pin(17)


def test_connect_default_pull_up_false_sets_pull_down():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(pull_up=False), emit=Mock())

    reader._connect()

    assert Device.pin_factory.pin(4).pull == "down"


def test_connect_pull_up_true_sets_pull_up():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(pull_up=True), emit=Mock())

    reader._connect()

    assert Device.pin_factory.pin(4).pull == "up"


def test_connect_bounce_ms_positive_is_passed_as_seconds():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(bounce_ms=50), emit=Mock())

    reader._connect()

    assert Device.pin_factory.pin(4).bounce == 0.05


def test_connect_bounce_ms_zero_passes_no_bounce_filtering():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(bounce_ms=0), emit=Mock())

    reader._connect()

    assert Device.pin_factory.pin(4).bounce is None


def test_connect_without_pin_factory_raises_bad_pin_factory():
    """Khong co MockFactory (va khong co Pi that) - DigitalInputDevice() phai
    raise (BadPinFactory tren gpiozero that), khong duoc nuot loi im lang."""
    Device.pin_factory = None
    reader = GpioReader(_cfg(), emit=Mock())

    with pytest.raises(Exception):
        reader._connect()

    assert reader._device is None


# ----------------------------------------------------------------------
# 3) GpioReader._read_once() - dung MockFactory THAT drive_high()/drive_low()

def test_read_once_returns_1_when_pin_driven_high():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(), emit=Mock())
    reader._connect()
    pin = Device.pin_factory.pin(4)

    pin.drive_high()

    assert reader._read_once() == 1.0
    reader._device.close()


def test_read_once_returns_0_when_pin_driven_low():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(), emit=Mock())
    reader._connect()
    pin = Device.pin_factory.pin(4)

    pin.drive_low()

    assert reader._read_once() == 0.0
    reader._device.close()


def test_read_once_invert_true_flips_high_to_0_and_low_to_1():
    Device.pin_factory = MockFactory()
    reader = GpioReader(_cfg(invert=True), emit=Mock())
    reader._connect()
    pin = Device.pin_factory.pin(4)

    pin.drive_high()
    assert reader._read_once() == 0.0

    pin.drive_low()
    assert reader._read_once() == 1.0

    reader._device.close()


# ----------------------------------------------------------------------
# 4) GpioReader._run() - vong lap thread that (mock _connect/_read_once,
#    giong het pattern cua modbus.py)

def test_run_retries_connect_with_increasing_backoff():
    reader = GpioReader(_cfg(poll_ms=1000), emit=Mock())
    attempts = {"n": 0}

    def fake_connect():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise ConnectionError("khong khoi tao duoc pin")
        reader._stop.set()

    waits = []

    def fake_wait(timeout=None):
        waits.append(timeout)
        return False

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reader, "_connect", fake_connect)
        mp.setattr(reader._stop, "wait", fake_wait)
        reader._run()

    assert waits == [1.0, 2.0, 4.0]
    assert attempts["n"] == 4


def test_run_connect_failure_sets_status_error_and_offline():
    reader = GpioReader(_cfg(poll_ms=1000), emit=Mock())

    def fake_connect():
        reader._stop.set()
        raise RuntimeError("BadPinFactory: khong co pin factory")

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reader, "_connect", fake_connect)
        mp.setattr(reader._stop, "wait", lambda timeout=None: False)
        reader._run()

    assert reader.status.online is False
    assert "BadPinFactory" in reader.status.error


def test_run_backoff_applies_to_read_failures_not_just_connect_failures():
    """Regression - cung pattern voi modbus.py: neu pin CONNECT duoc nhung
    DOC LUON LOI, backoff van phai duoc ap dung (KHONG duoc reset som truoc
    vong doc, va nhanh loi doc PHAI goi self._stop.wait() truoc khi break),
    tranh busy-loop connect+read. Chay trong thread rieng + join(timeout=...)
    de khong treo ca suite neu regression tai xuat (vong lap chay vo han)."""
    reader = GpioReader(_cfg(poll_ms=1000), emit=Mock())
    waits = []

    def fake_wait(timeout=None):
        waits.append(timeout)
        if len(waits) >= 4:
            reader._stop.set()
        return False

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reader, "_connect", lambda: None)
        mp.setattr(reader, "_read_once", Mock(side_effect=IOError("loi doc GPIO")))
        mp.setattr(reader._stop, "wait", fake_wait)
        t = threading.Thread(target=reader._run, daemon=True)
        t.start()
        t.join(timeout=2)
        reader._stop.set()  # phong khi wait() khong duoc goi (regression)
        t.join(timeout=1)

    assert not t.is_alive(), (
        "reader._run() van dang chay thread sau timeout - nhanh loi doc "
        "khong goi self._stop.wait() nen vong lap chay VO HAN (busy-loop)")
    assert waits == [1.0, 2.0, 4.0, 8.0]


def test_run_emits_none_and_backoff_resets_after_successful_read():
    """Doc loi 1 lan (backoff tang) roi doc thanh cong lien tuc - backoff
    phai VE LAI _BACKOFF_MIN_S ngay sau lan doc thanh cong dau tien (KHONG
    giu backoff da tang tu lan loi truoc)."""
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    reader = GpioReader(_cfg(poll_ms=10), emit=fake_emit)
    read_results = iter([IOError("loi doc 1 lan"), 1.0, 1.0])

    def fake_read_once():
        result = next(read_results)
        if isinstance(result, Exception):
            raise result
        return result

    waits = []

    def fake_wait(timeout=None):
        waits.append(timeout)
        if len(waits) >= 1:
            # sau backoff dau tien (do loi doc), cho 2 lan doc thanh cong
            # roi dung han lap - _stop.wait() con duoc goi giua cac lan doc
            # thanh cong (khoang nghi poll_ms) nen chi dung khi da co it nhat
            # 2 gia tri emit thanh cong.
            success_reads = [e for e in emitted if e[3]]
            if len(success_reads) >= 2:
                reader._stop.set()
        return False

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reader, "_connect", lambda: None)
        mp.setattr(reader, "_read_once", fake_read_once)
        mp.setattr(reader._stop, "wait", fake_wait)
        reader._run()

    assert emitted[0] == ("ch1", None, 2, False)
    assert emitted[1] == ("ch1", 1.0, 0, True)
    assert emitted[2] == ("ch1", 1.0, 0, True)
    # backoff dau tien = 1.0 (loi doc), cac wait() sau do (giua cac lan doc
    # thanh cong) phai la khoang nghi poll (max(0.05, poll_ms/1000) = 0.05s
    # voi poll_ms=10), KHONG phai backoff da tang tiep (2.0) - tuc backoff
    # da RESET ve _BACKOFF_MIN_S sau doc OK.
    assert waits[0] == 1.0
    assert waits[1] == pytest.approx(0.05)


def test_run_closes_device_on_read_failure_before_reconnecting():
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    reader = GpioReader(_cfg(poll_ms=10), emit=fake_emit)
    close_calls = []
    connect_calls = {"n": 0}

    def fake_connect():
        connect_calls["n"] += 1
        reader._device = Mock()
        reader._device.close.side_effect = lambda: close_calls.append(True)
        if connect_calls["n"] >= 2:
            reader._stop.set()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(reader, "_connect", fake_connect)
        mp.setattr(reader, "_read_once", Mock(side_effect=IOError("loi doc GPIO")))
        mp.setattr(reader._stop, "wait", lambda timeout=None: False)
        reader._run()

    assert ("ch1", None, 2, False) in emitted
    assert close_calls
    assert connect_calls["n"] == 2


def test_run_stops_cleanly_and_closes_device_with_real_mock_factory():
    """Tich hop that: chay _run() trong thread rieng voi MockFactory THAT
    (khong mock _connect/_read_once) - dung() phai dong thread sach va goi
    close() tren device that (KHONG con giu tham chieu device)."""
    Device.pin_factory = MockFactory()
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    reader = GpioReader(_cfg(poll_ms=10), emit=fake_emit)
    t = threading.Thread(target=reader._run, daemon=True)
    t.start()

    assert _wait_until(lambda: reader.status.online), \
        "reader khong online sau khi _connect() voi MockFactory"
    assert _wait_until(lambda: len(emitted) >= 1), "khong emit gia tri nao"

    reader._stop.set()
    t.join(timeout=2)

    assert not t.is_alive()
    assert reader._device is None


def test_run_full_integration_reads_real_pin_transitions_via_mock_factory():
    """Tich hop that xuyen suot: KHONG mock _connect/_read_once - drive_high()/
    drive_low() tren pin MockFactory that, xac nhan _run() emit dung gia tri
    tuong ung qua toan bo vong lap that."""
    Device.pin_factory = MockFactory()
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    reader = GpioReader(_cfg(pin=17, poll_ms=10), emit=fake_emit)
    t = threading.Thread(target=reader._run, daemon=True)
    t.start()

    assert _wait_until(lambda: reader.status.online), "reader khong online"
    pin = Device.pin_factory.pin(17)

    mark = len(emitted)
    pin.drive_high()
    assert _wait_until(lambda: len(emitted) > mark and emitted[-1][1] == 1.0), \
        "khong thay gia tri 1.0 sau drive_high()"

    mark = len(emitted)
    pin.drive_low()
    assert _wait_until(lambda: len(emitted) > mark and emitted[-1][1] == 0.0), \
        "khong thay gia tri 0.0 sau drive_low()"

    reader._stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
