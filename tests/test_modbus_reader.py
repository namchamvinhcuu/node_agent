# -*- coding: utf-8 -*-
"""Test node_agent/readers/modbus.py (ModbusReader) - reader Modbus RTU/TCP
moi, CHUA co thiet bi Modbus that trong moi truong dev (Nam xac nhan) nen
toan bo test o day dung mock cho pymodbus (khong co PLC/bien tan that).

Luu y ve mock: ModbusReader import ModbusTcpClient/ModbusSerialClient o
MODULE-LEVEL trong node_agent/readers/modbus.py (khong con lazy-import ben
trong _connect() nhu ban dau - da chuyen len module-level sau finding cua
python-reviewer 2026-09-25: lazy-import khong con ly do chinh dang vi
pymodbus la dependency BAT BUOC, va loi ImportError bi nuot nham thanh
"khong ket noi duoc" trong vong retry). Vi vay phai patch dung namespace SU
DUNG `node_agent.readers.modbus.ModbusTcpClient`/`ModbusSerialClient`
(KHONG phai `pymodbus.client.ModbusTcpClient` - patch tai nguon dinh nghia
se KHONG co tac dung len ten da bind vao module modbus.py tu luc import)."""
import threading
import time
from unittest.mock import MagicMock, Mock, patch

import pytest

from node_agent.readers.modbus import ModbusReader, _decode, _encode


# ----------------------------------------------------------------------
# 1) _decode / _encode - round-trip cho moi data_type

@pytest.mark.parametrize("value", [0, 1, 1000, 65535])
def test_decode_u16(value):
    assert _decode([value], "u16") == value


@pytest.mark.parametrize("value,expected", [(0, 0), (1000, 1000), (32767, 32767)])
def test_decode_i16_positive(value, expected):
    assert _decode([value], "i16") == expected


@pytest.mark.parametrize("raw_reg,expected", [(0x8000, -32768), (0xFFFF, -1), (0x8001, -32767)])
def test_decode_i16_negative_checks_sign_bit_ge_0x8000(raw_reg, expected):
    """i16 am - _decode phai kiem tra bit dau (regs[0] >= 0x8000) roi tru
    0x10000 de ra gia tri am dung, khong duoc tra thang gia tri unsigned."""
    assert _decode([raw_reg], "i16") == expected


@pytest.mark.parametrize("value", [0, 1, 70000, 4294967295])
def test_decode_u32(value):
    hi, lo = _encode("u32", value)
    assert _decode([hi, lo], "u32") == value


@pytest.mark.parametrize("value", [0, 1, -1, 70000, -70000, 2147483647, -2147483648])
def test_decode_i32(value):
    hi, lo = _encode("i32", value)
    assert _decode([hi, lo], "i32") == value


@pytest.mark.parametrize("value", [0.0, 1.5, -1.5, 3.14159, -12345.678])
def test_decode_f32_round_trip(value):
    hi, lo = _encode("f32", value)
    decoded = _decode([hi, lo], "f32")
    assert decoded == pytest.approx(value, rel=1e-5)


def test_decode_defaults_to_u16_when_dtype_missing():
    assert _decode([42], None) == 42
    assert _decode([42], "") == 42


# ----------------------------------------------------------------------
# 2) _encode - so luong register dung + round-trip qua _decode

@pytest.mark.parametrize("dtype", ["u16", "i16"])
def test_encode_16bit_returns_single_register(dtype):
    regs = _encode(dtype, 100)
    assert len(regs) == 1


@pytest.mark.parametrize("dtype", ["u32", "i32", "f32"])
def test_encode_32bit_returns_two_registers(dtype):
    regs = _encode(dtype, 100)
    assert len(regs) == 2


@pytest.mark.parametrize("dtype,value", [
    ("u16", 0), ("u16", 65535), ("u16", 1234),
    ("i16", 0), ("i16", -1), ("i16", -32768), ("i16", 32767),
    ("u32", 0), ("u32", 4294967295), ("u32", 123456),
    ("i32", 0), ("i32", -1), ("i32", -2147483648), ("i32", 2147483647),
])
def test_encode_decode_round_trip_matches_original_value(dtype, value):
    regs = _encode(dtype, value)
    assert _decode(regs, dtype) == value


@pytest.mark.parametrize("value", [0.0, 1.25, -1.25, 999.999])
def test_encode_decode_round_trip_f32_approx(value):
    regs = _encode("f32", value)
    assert _decode(regs, "f32") == pytest.approx(value, rel=1e-5)


def test_encode_negative_i16_wraps_to_unsigned_16bit_representation():
    """-1 phai encode thanh 0xFFFF (two's complement 16-bit), khop voi
    _decode i16 doc lai dung -1."""
    regs = _encode("i16", -1)
    assert regs == [0xFFFF]


@pytest.mark.parametrize("raw", [2.9999999999999996, 5.999999999999999, 6.999999999999999,
                                  11.999999999999998, 13.999999999999998, 18.999999999999996,
                                  22.999999999999996, 23.999999999999996])
def test_encode_rounds_instead_of_truncating_floating_point_noise(raw):
    """Regression cho finding python-reviewer 2026-09-25: _encode() TRUOC
    DAY dung int(value) (truncate ve 0) thay vi round() - voi scale=0.1
    (dung scale that su cua kenh vfd_speed trong channels.example.json),
    raw = value/scale co the la vd 2.9999999999999996 thay vi 3.0 dung do
    sai so dau phay dong khi chia cho 0.1 - int() cat thanh 2 (SAI 1 don vi
    thanh ghi so voi gia tri Nam dinh ghi/doc = 3), round() cho dung 3. Cac
    gia tri raw o day la mau THAT lay tu quet 0.0..2000.0 buoc 0.1 (6457/
    20001 gia tri sai neu dung int(), 0 sai neu dung round() - xem test day
    du hon o test_command_write_scale_0_1_no_off_by_one_across_full_range)."""
    intended = round(raw)
    regs_16 = _encode("u16", raw)
    assert _decode(regs_16, "u16") == intended
    regs_32 = _encode("u32", raw)
    assert _decode(regs_32, "u32") == intended
    regs_i32 = _encode("i32", raw)
    assert _decode(regs_i32, "i32") == intended


# ----------------------------------------------------------------------
# 3) ModbusReader._connect()

def _tcp_cfg(**overrides):
    cfg = {"code": "ch1", "conn_type": "tcp", "host": "10.0.0.5", "tcp_port": 502}
    cfg.update(overrides)
    return cfg


def _rtu_cfg(**overrides):
    cfg = {"code": "ch1", "conn_type": "rtu", "port": "/dev/ttyUSB1", "baud": 19200}
    cfg.update(overrides)
    return cfg


def test_connect_tcp_success_creates_client_with_host_and_port():
    reader = ModbusReader(_tcp_cfg(), emit=Mock())
    fake_client = MagicMock()
    fake_client.connect.return_value = True

    with patch("node_agent.readers.modbus.ModbusTcpClient", return_value=fake_client) as mock_tcp:
        reader._connect()

    mock_tcp.assert_called_once_with("10.0.0.5", port=502)
    assert reader._client is fake_client


def test_connect_rtu_success_creates_client_with_port_and_baudrate():
    reader = ModbusReader(_rtu_cfg(), emit=Mock())
    fake_client = MagicMock()
    fake_client.connect.return_value = True

    with patch("node_agent.readers.modbus.ModbusSerialClient", return_value=fake_client) as mock_serial:
        reader._connect()

    mock_serial.assert_called_once_with("/dev/ttyUSB1", baudrate=19200)
    assert reader._client is fake_client


def test_connect_tcp_failure_raises_connection_error():
    reader = ModbusReader(_tcp_cfg(), emit=Mock())
    fake_client = MagicMock()
    fake_client.connect.return_value = False

    with patch("node_agent.readers.modbus.ModbusTcpClient", return_value=fake_client):
        with pytest.raises(ConnectionError):
            reader._connect()


def test_connect_rtu_failure_raises_connection_error():
    reader = ModbusReader(_rtu_cfg(), emit=Mock())
    fake_client = MagicMock()
    fake_client.connect.return_value = False

    with patch("node_agent.readers.modbus.ModbusSerialClient", return_value=fake_client):
        with pytest.raises(ConnectionError):
            reader._connect()


# ----------------------------------------------------------------------
# 4) ModbusReader._read_once()

def _reader_with_fake_client(cfg_overrides=None, client=None):
    cfg = _tcp_cfg(**(cfg_overrides or {}))
    reader = ModbusReader(cfg, emit=Mock())
    reader._client = client or MagicMock()
    return reader


def test_read_once_holding_register_decodes_and_applies_scale_offset():
    reader = _reader_with_fake_client({"register_type": "holding", "data_type": "u16",
                                        "address": 10, "unit_id": 3, "scale": 2.0, "offset": 5.0})
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [100]
    reader._client.read_holding_registers.return_value = rr

    value = reader._read_once()

    reader._client.read_holding_registers.assert_called_once_with(10, count=1, device_id=3)
    assert value == 100 * 2.0 + 5.0


def test_read_once_input_register_calls_read_input_registers():
    reader = _reader_with_fake_client({"register_type": "input", "data_type": "u16",
                                        "address": 20, "unit_id": 1})
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [42]
    reader._client.read_input_registers.return_value = rr

    value = reader._read_once()

    reader._client.read_input_registers.assert_called_once_with(20, count=1, device_id=1)
    assert value == 42


def test_read_once_32bit_dtype_reads_two_registers():
    reader = _reader_with_fake_client({"register_type": "holding", "data_type": "u32", "address": 0})
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = _encode("u32", 70000)
    reader._client.read_holding_registers.return_value = rr

    value = reader._read_once()

    reader._client.read_holding_registers.assert_called_once_with(0, count=2, device_id=1)
    assert value == 70000


def test_read_once_raises_io_error_when_response_is_error():
    reader = _reader_with_fake_client({"register_type": "holding", "data_type": "u16"})
    rr = MagicMock()
    rr.isError.return_value = True
    rr.__str__.return_value = "ExceptionResponse(...)"
    reader._client.read_holding_registers.return_value = rr

    with pytest.raises(IOError):
        reader._read_once()


def test_read_once_default_scale_offset_is_identity():
    reader = _reader_with_fake_client({"register_type": "holding", "data_type": "u16", "address": 0})
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [77]
    reader._client.read_holding_registers.return_value = rr

    assert reader._read_once() == 77


# ----------------------------------------------------------------------
# 5) ModbusReader._run() - vong lap thread that

def test_run_retries_connect_with_increasing_backoff():
    """3 lan _connect() dau that bai roi thanh cong - self._stop.wait() phai
    duoc goi voi backoff TANG DAN (1.0 -> 2.0 -> 4.0), khong duoc giu nguyen
    hang so hay giam."""
    reader = ModbusReader(_tcp_cfg(poll_ms=1000), emit=Mock())
    attempts = {"n": 0}

    def fake_connect():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise ConnectionError("khong ket noi duoc")
        reader._stop.set()  # thanh cong lan 4 - dung vong lap ngay, khong doc gi them

    waits = []

    def fake_wait(timeout=None):
        waits.append(timeout)
        return False

    with patch.object(reader, "_connect", side_effect=fake_connect), \
         patch.object(reader._stop, "wait", side_effect=fake_wait):
        reader._run()

    assert waits == [1.0, 2.0, 4.0]
    assert attempts["n"] == 4


def test_run_backoff_applies_to_read_failures_not_just_connect_failures():
    """Regression cho finding python-reviewer 2026-09-25: TRUOC DAY backoff
    bi reset ve _BACKOFF_MIN_S NGAY SAU connect thanh cong (truoc khi vao
    vong doc), va nhanh loi doc (`except` trong vong doc) KHONG goi
    `self._stop.wait(...)` truoc khi break - neu thiet bi CONNECT duoc
    nhung DOC LUON LOI (sai address/register_type/unit_id, hoac PLC tra
    exception response co dinh) thi KHONG co backoff nao ca -> busy-loop
    connect+read (verify thuc nghiem cua reviewer: 1.1 trieu lan/giay).
    Fix dung: backoff CHI reset sau khi DOC THANH CONG, va nhanh loi doc
    cung phai wait(backoff) + tang backoff truoc khi break/reconnect,
    giong het nhanh connect-fail.

    Chay trong THREAD RIENG voi join(timeout=...) thay vi goi _run() truc
    tiep tren thread test: neu bug tai xuat hien (nhanh loi doc khong bao
    gio goi self._stop.wait()), vong lap se chay VO HAN vi khong co diem
    nao kiem tra self._stop.is_set() giua 2 lan goi _connect()/_read_once()
    - goi truc tiep se treo CA SUITE vinh vien (da tu kiem chung khi mutate
    thu: phai Ctrl-C/timeout kill process). Thread + join(timeout) + assert
    not t.is_alive() bien regression do thanh 1 FAIL ro rang thay vi hang."""
    reader = ModbusReader(_tcp_cfg(poll_ms=1000), emit=Mock())
    waits = []

    def fake_wait(timeout=None):
        waits.append(timeout)
        if len(waits) >= 4:
            reader._stop.set()
        return False

    with patch.object(reader, "_connect", lambda: None), \
         patch.object(reader, "_read_once", side_effect=IOError("loi doc modbus")), \
         patch.object(reader._stop, "wait", side_effect=fake_wait):
        t = threading.Thread(target=reader._run, daemon=True)
        t.start()
        t.join(timeout=2)
        reader._stop.set()  # phong khi wait() khong duoc goi (regression) - ep dung thread
        t.join(timeout=1)

    assert not t.is_alive(), (
        "reader._run() van dang chay thread sau timeout - nhanh loi doc "
        "khong goi self._stop.wait() nen vong lap connect+read chay VO HAN "
        "(busy-loop), day chinh la bug python-reviewer 2026-09-25 da bao")
    assert waits == [1.0, 2.0, 4.0, 8.0]


def test_run_emits_values_in_background_thread_then_stops_cleanly():
    """Chay _run() trong thread rieng (nhu production that: start() tao
    thread), stop sau vai lan lap qua timeout ngan."""
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    reader = ModbusReader(_tcp_cfg(poll_ms=10), emit=fake_emit)

    with patch.object(reader, "_connect", lambda: None), \
         patch.object(reader, "_read_once", side_effect=[1.0, 2.0, 3.0, 4.0, 5.0] * 50):
        t = threading.Thread(target=reader._run, daemon=True)
        t.start()
        time.sleep(0.05)
        reader._stop.set()
        t.join(timeout=2)

    assert not t.is_alive()
    assert len(emitted) >= 1
    assert emitted[0] == ("ch1", 1.0, 0, True)


def test_run_emits_none_and_breaks_inner_loop_on_read_error():
    """_read_once() loi -> emit(code, None, None, 2, False) roi break khoi
    vong doc, quay lai ket noi lai (khong crash ca thread)."""
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    reader = ModbusReader(_tcp_cfg(poll_ms=10), emit=fake_emit)
    close_calls = []
    connect_calls = {"n": 0}

    def fake_connect():
        connect_calls["n"] += 1
        reader._client = MagicMock()
        reader._client.close.side_effect = lambda: close_calls.append(True)
        if connect_calls["n"] >= 2:
            reader._stop.set()

    with patch.object(reader, "_connect", side_effect=fake_connect), \
         patch.object(reader, "_read_once", side_effect=IOError("loi doc modbus")), \
         patch.object(reader._stop, "wait", return_value=False):
        reader._run()

    assert ("ch1", None, 2, False) in emitted
    assert close_calls  # client phai duoc close() sau khi break khoi vong doc
    assert connect_calls["n"] == 2


# ----------------------------------------------------------------------
# 6) ModbusReader.command("write", value)

def test_command_write_16bit_holding_register_success():
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="u16",
                                    address=15, unit_id=2), emit=Mock())
    reader._client = MagicMock()

    result = reader.command("write", 123)

    reader._client.write_register.assert_called_once_with(15, 123, device_id=2)
    reader._client.write_registers.assert_not_called()
    assert result == {"ok": True, "status": "ok"}


def test_command_write_32bit_holding_register_calls_write_registers():
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="f32",
                                    address=20, unit_id=1), emit=Mock())
    reader._client = MagicMock()

    result = reader.command("write", 3.5)

    reader._client.write_registers.assert_called_once()
    call_args = reader._client.write_registers.call_args
    assert call_args.args[0] == 20
    assert len(call_args.args[1]) == 2
    assert call_args.kwargs == {"device_id": 1}
    reader._client.write_register.assert_not_called()
    assert result == {"ok": True, "status": "ok"}


def test_command_write_applies_scale_and_offset_inverse_before_encode():
    """command() phai ap dung nghich dao scale/offset (raw = (value-offset)/
    scale) truoc khi encode, doi xung voi _read_once() ap scale/offset
    THUAN (value = raw*scale+offset)."""
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="u16",
                                    address=0, unit_id=1, scale=2.0, offset=10.0), emit=Mock())
    reader._client = MagicMock()

    reader.command("write", 30)  # raw = (30-10)/2 = 10

    reader._client.write_register.assert_called_once_with(0, 10, device_id=1)


def test_command_write_rejects_input_register_as_read_only():
    reader = ModbusReader(_tcp_cfg(register_type="input"), emit=Mock())
    reader._client = MagicMock()

    result = reader.command("write", 1)

    assert result["ok"] is False
    assert "read-only" in result["error"]
    reader._client.write_register.assert_not_called()
    reader._client.write_registers.assert_not_called()


def test_command_write_fails_when_not_connected():
    reader = ModbusReader(_tcp_cfg(register_type="holding"), emit=Mock())
    reader._client = None

    result = reader.command("write", 1)

    assert result["ok"] is False
    assert "chua ket noi" in result["error"]


def test_command_rejects_non_write_cmd():
    reader = ModbusReader(_tcp_cfg(), emit=Mock())
    reader._client = MagicMock()

    result = reader.command("read")

    assert result["ok"] is False


def test_command_write_without_value_is_rejected():
    reader = ModbusReader(_tcp_cfg(), emit=Mock())
    reader._client = MagicMock()

    result = reader.command("write", None)

    assert result["ok"] is False


def test_command_write_scale_0_1_no_off_by_one_across_full_range():
    """Regression cho finding python-reviewer 2026-09-25 - quet toan bo dai
    vat ly 0.0..2000.0 buoc 0.1 (dung scale=0.1/offset=0/data_type=u16 cua
    kenh vfd_speed that trong channels.example.json) qua chinh public API
    command("write", ...): moi gia tri ghi xuong thanh ghi phai khop CHINH
    XAC round(value*10), khong duoc lech 1 don vi nhu bug int() truoc day
    (6457/20001 gia tri sai neu dung int(), 0 sai neu dung round())."""
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="u16",
                                    address=0, unit_id=1, scale=0.1, offset=0), emit=Mock())
    reader._client = MagicMock()
    mismatches = []
    v = 0.0
    while v <= 2000.0001:
        reader._client.write_register.reset_mock()
        reader.command("write", v)
        written = reader._client.write_register.call_args.args[1]
        expected = round(v * 10)
        if written != expected:
            mismatches.append((v, written, expected))
        v = round(v + 0.1, 4)

    assert mismatches == []


def test_command_write_catches_exception_from_client_and_returns_error_dict():
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="u16"), emit=Mock())
    reader._client = MagicMock()
    reader._client.write_register.side_effect = RuntimeError("bus loi")

    result = reader.command("write", 5)

    assert result["ok"] is False
    assert "bus loi" in result["error"]
