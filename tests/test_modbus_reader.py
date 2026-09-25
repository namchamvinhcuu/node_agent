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
se KHONG co tac dung len ten da bind vao module modbus.py tu luc import).

Cap nhat 2026-09-25 (shared-client registry, fix bug that tren OrangePi3B
"[Errno 11] Could not exclusively lock port"): `ModbusReader._connect()`
KHONG CON TON TAI - thay bang `_client_key`/`_client_factory` (tinh san
trong __init__) + module-level `_SharedModbusClient`/`_get_shared_client()`
(registry theo key). `_run()` goi `_get_shared_client(...).acquire()` MOT
LAN luc bat dau, vong lap ngoai goi `self._client.ensure_connected()` (KHONG
phai `self._connect()`), vong lap trong KHONG con dong client khi loi doc.
Xem docstring dau file `node_agent/readers/modbus.py` de biet ly do."""
import threading
import time
from unittest.mock import MagicMock, Mock, patch

import pytest

from node_agent.readers.modbus import (
    ModbusReader,
    _decode,
    _encode,
    _get_shared_client,
    _MAX_CONSECUTIVE_ERRORS,
    _shared_clients,
    _SharedModbusClient,
)


@pytest.fixture(autouse=True)
def _clear_shared_client_registry():
    """Registry `_shared_clients` la module-level dict, TON TAI xuyen suot
    process - khong clear se lam test SAU dung lai _SharedModbusClient (voi
    client/mock cu) ma test TRUOC da tao ra cung 1 client_key (vd _tcp_cfg()
    mac dinh luon la ("tcp", "10.0.0.5", 502)), gay test order-dependency
    (flaky/sai lech tuy thu tu chay)."""
    _shared_clients.clear()
    yield
    _shared_clients.clear()


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
# 2b) ModbusReader.__init__() - ep kieu poll_ms (cung lop bug da fix o
#     readers/gpio.py, Phase 3 - xem test_gpio_reader.py::
#     test_init_poll_ms_non_numeric_string_raises_value_error)

def test_init_poll_ms_non_numeric_string_raises_value_error():
    """Regression: channels.json sua tay ghi poll_ms khong parse duoc (vd
    "abc") PHAI raise ValueError NGAY trong __init__ (duoc agent.py::
    _build_readers() bat va skip kenh) - KHONG duoc de lot vao _run() roi
    thread chet im lang giua vong lap (status.online da la True tu truoc do
    -> health-check bao SAI la kenh van online)."""
    with pytest.raises(ValueError):
        ModbusReader(_tcp_cfg(poll_ms="abc"), emit=Mock())


def test_init_poll_ms_numeric_string_parses_correctly():
    """Khong phai case fail: poll_ms dang chuoi so hop le ("1000") van phai
    parse duoc binh thuong, KHONG raise."""
    reader = ModbusReader(_tcp_cfg(poll_ms="1000"), emit=Mock())

    assert reader._poll_s == 1.0


# ----------------------------------------------------------------------
# 3) ModbusReader.__init__() - _client_key / _client_factory (thay the
#    _connect() cu da bi xoa - xem docstring dau file)

def _tcp_cfg(**overrides):
    cfg = {"code": "ch1", "conn_type": "tcp", "host": "10.0.0.5", "tcp_port": 502}
    cfg.update(overrides)
    return cfg


def _rtu_cfg(**overrides):
    cfg = {"code": "ch1", "conn_type": "rtu", "port": "/dev/ttyUSB1", "baud": 19200}
    cfg.update(overrides)
    return cfg


def test_init_tcp_computes_client_key_from_host_and_port():
    reader = ModbusReader(_tcp_cfg(), emit=Mock())

    assert reader._client_key == ("tcp", "10.0.0.5", 502)


def test_init_rtu_computes_client_key_from_port_and_baud():
    reader = ModbusReader(_rtu_cfg(), emit=Mock())

    assert reader._client_key == ("rtu", "/dev/ttyUSB1", 19200)


def test_client_factory_tcp_creates_client_with_host_and_port():
    reader = ModbusReader(_tcp_cfg(), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.modbus.ModbusTcpClient", return_value=fake_client) as mock_tcp:
        client = reader._client_factory()

    mock_tcp.assert_called_once_with("10.0.0.5", port=502)
    assert client is fake_client


def test_client_factory_rtu_creates_client_with_port_and_baudrate():
    reader = ModbusReader(_rtu_cfg(), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.modbus.ModbusSerialClient", return_value=fake_client) as mock_serial:
        client = reader._client_factory()

    mock_serial.assert_called_once_with("/dev/ttyUSB1", baudrate=19200)
    assert client is fake_client


# ----------------------------------------------------------------------
# 3b) _SharedModbusClient - acquire/release refcount, ensure_connected,
#     _io_lock serialize I/O

def _fake_underlying_client(connected=True):
    client = MagicMock()
    client.is_socket_open.return_value = connected
    client.connect.return_value = True
    return client


def test_shared_client_ensure_connected_calls_connect_when_socket_not_open():
    fake_client = _fake_underlying_client(connected=False)
    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()

    shared.ensure_connected()

    fake_client.connect.assert_called_once()


def test_shared_client_ensure_connected_skips_connect_when_socket_already_open():
    fake_client = _fake_underlying_client(connected=True)
    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()

    shared.ensure_connected()

    fake_client.connect.assert_not_called()


def test_shared_client_ensure_connected_raises_connection_error_when_connect_fails():
    fake_client = _fake_underlying_client(connected=False)
    fake_client.connect.return_value = False
    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()

    with pytest.raises(ConnectionError):
        shared.ensure_connected()


def test_shared_client_ensure_connected_raises_when_client_already_released():
    shared = _SharedModbusClient(factory=lambda: _fake_underlying_client())
    # KHONG goi acquire() - self._client con la None (giong trang thai sau
    # khi refcount ve 0 va release() da dong+xoa client).

    with pytest.raises(ConnectionError):
        shared.ensure_connected()


def test_shared_client_acquire_release_refcount_closes_only_on_last_release():
    """2 lan acquire() (2 ModbusReader dung chung) + 1 lan release() -> client
    CHUA duoc dong (con reader kia dang dung). Release lan 2 (refcount ve 0)
    moi thuc su dong+xoa client."""
    fake_client = _fake_underlying_client()
    shared = _SharedModbusClient(factory=lambda: fake_client)

    shared.acquire()
    shared.acquire()
    shared.release()

    fake_client.close.assert_not_called()
    assert shared._client is fake_client

    shared.release()

    fake_client.close.assert_called_once()
    assert shared._client is None


def test_shared_client_io_lock_serializes_concurrent_reads_no_overlap():
    """2 thread goi read_holding_registers() DONG THOI tren CUNG 1
    _SharedModbusClient - _io_lock phai serial hoa, khong duoc chong lap
    thoi gian thuc thi (RTU la half-duplex, GIL Python khong bao ve duoc
    chuyen nay - day la gioi han giao thuc serial, khong phai gioi han ngon
    ngu - xem docstring dau file modbus.py)."""
    intervals = []
    intervals_lock = threading.Lock()

    def slow_read(*args, **kwargs):
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        with intervals_lock:
            intervals.append((start, end))
        return Mock()

    fake_client = _fake_underlying_client()
    fake_client.read_holding_registers.side_effect = slow_read
    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()

    t1 = threading.Thread(target=shared.read_holding_registers, args=(0,), kwargs={"count": 1})
    t2 = threading.Thread(target=shared.read_holding_registers, args=(0,), kwargs={"count": 1})
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert len(intervals) == 2
    (s1, e1), (s2, e2) = intervals
    # Khong overlap: 1 khoang phai ket thuc TRUOC khi khoang kia bat dau.
    assert e1 <= s2 or e2 <= s1, f"2 lan doc chong lap thoi gian: {intervals}"


# ----------------------------------------------------------------------
# 3b-2) Regression cho 3 finding python-reviewer 2026-09-25 (sua boi main,
#       KHONG phai boi test-writer - xem docstring class _SharedModbusClient
#       trong modbus.py: release()/ensure_connected()/acquire()/_call())

def test_shared_client_release_closes_only_after_pending_io_completes_no_lock_nesting():
    """Regression Major #1: TRUOC DAY release() goi close() trong luc GIU
    _registry_lock ma KHONG giu _io_lock - co the chay SONG SONG voi 1 lenh
    doc/ghi dang do dang tren CUNG client (vd command() dang write_register()
    dung luc reader cuoi cung goi release()). Fix: giam refcount duoi
    _registry_lock, tinh should_close, RA KHOI _registry_lock moi close()
    duoi _io_lock RIENG - dam bao close() KHONG BAO GIO chay xen giua luc 1
    lenh I/O dang thuc thi (ca hai deu phai giu cung _io_lock).

    Verify bang 2 thread that: 1 thread dang "write" cham (mock sleep 0.05s),
    1 thread khac goi release() ngay sau do - assert thu tu ghi nhan la
    write_start -> write_end -> close (khong bao gio la write_start ->
    close -> write_end, chung minh khong lot lock)."""
    order = []
    order_lock = threading.Lock()

    def slow_write(*args, **kwargs):
        with order_lock:
            order.append("write_start")
        time.sleep(0.05)
        with order_lock:
            order.append("write_end")
        return Mock()

    def record_close():
        with order_lock:
            order.append("close")

    fake_client = _fake_underlying_client()
    fake_client.write_register.side_effect = slow_write
    fake_client.close.side_effect = record_close

    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()  # refcount = 1

    write_thread = threading.Thread(target=shared.write_register, args=(0, 1), kwargs={"device_id": 1})
    write_thread.start()
    time.sleep(0.02)  # dam bao write_start da duoc ghi truoc khi release() chay
    release_thread = threading.Thread(target=shared.release)
    release_thread.start()
    write_thread.join(timeout=2)
    release_thread.join(timeout=2)

    assert not write_thread.is_alive()
    assert not release_thread.is_alive()
    assert order == ["write_start", "write_end", "close"], (
        f"close() da xen vao giua 1 lenh I/O dang chay (lock nesting sai): {order}")
    assert shared._client is None


def test_shared_client_call_increments_consecutive_errors_on_exception():
    fake_client = _fake_underlying_client()
    fake_client.read_holding_registers.side_effect = IOError("timeout")
    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()

    for _ in range(_MAX_CONSECUTIVE_ERRORS):
        with pytest.raises(IOError):
            shared.read_holding_registers(0, count=1, device_id=1)

    assert shared._consecutive_errors == _MAX_CONSECUTIVE_ERRORS


def test_shared_client_call_resets_consecutive_errors_on_success():
    """`_consecutive_errors` KHONG duoc tich luy xuyen qua 1 lan goi THANH
    CONG xen giua - phai reset ve 0 ngay lap tuc, khong doi den khi dat
    nguong moi reset."""
    fake_client = _fake_underlying_client()
    fake_client.read_holding_registers.side_effect = [IOError("timeout"), IOError("timeout"), Mock()]
    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()

    with pytest.raises(IOError):
        shared.read_holding_registers(0, count=1, device_id=1)
    with pytest.raises(IOError):
        shared.read_holding_registers(0, count=1, device_id=1)
    assert shared._consecutive_errors == 2

    shared.read_holding_registers(0, count=1, device_id=1)  # lan nay thanh cong

    assert shared._consecutive_errors == 0


def test_shared_client_ensure_connected_force_closes_and_reconnects_after_max_consecutive_errors():
    """Regression QUAN TRONG NHAT cho finding Major #2 (bug that da verify
    thuc nghiem bang socat): pymodbus sync client KHONG tu dong chuyen
    is_socket_open() ve False du giao thuc bi 'treo' o tang ung dung (timeout
    lien tiep, count_until_disconnect am) - is_socket_open() van tra True
    mai mai (mo phong bang gia tri co dinh True cua mock TRUOC khi close()
    duoc goi that su - dung nhu bug that: OS socket van 'trong con', chi
    close() cuong che moi thuc su dat lai trang thai). Neu khong dem loi lien
    tiep va cuong che dong+mo lai sau _MAX_CONSECUTIVE_ERRORS lan, reader se
    treo VINH VIEN o muc giao thuc toi khi restart ca tien trinh."""
    fake_client = _fake_underlying_client(connected=True)
    fake_client.read_holding_registers.side_effect = IOError("timeout")
    fake_client.connect.return_value = True

    def _do_close():
        # Mo phong hanh vi THAT cua pymodbus: is_socket_open() CHI dung tra
        # False SAU KHI close() that su chay (truoc do, du giao thuc da
        # "treo", no van tra True - day chinh la bug Major #2).
        fake_client.is_socket_open.return_value = False

    fake_client.close.side_effect = _do_close

    shared = _SharedModbusClient(factory=lambda: fake_client)
    shared.acquire()

    for _ in range(_MAX_CONSECUTIVE_ERRORS):
        with pytest.raises(IOError):
            shared.read_holding_registers(0, count=1, device_id=1)
    assert shared._consecutive_errors == _MAX_CONSECUTIVE_ERRORS

    shared.ensure_connected()

    fake_client.close.assert_called_once()
    assert shared._consecutive_errors == 0
    fake_client.connect.assert_called_once()  # cuong che dong xong phai reconnect lai


def test_shared_client_acquire_does_not_increment_refcount_when_factory_raises():
    """Regression Minor: acquire() TRUOC DAY tang _refcount TRUOC khi goi
    factory() - neu factory() raise (vd port khong ton tai/khong du quyen),
    _refcount se bi LECH vinh vien (tang nhung client khong duoc tao, reader
    khac dung chung key sau nay se khong bao gio thay refcount ve 0 dung).
    Fix: tang refcount SAU khi factory() thanh cong."""
    def failing_factory():
        raise OSError("khong mo duoc cong")

    shared = _SharedModbusClient(factory=failing_factory)

    with pytest.raises(OSError):
        shared.acquire()

    assert shared._refcount == 0
    assert shared._client is None


def test_run_factory_raises_sets_error_status_and_returns_cleanly_without_crash():
    """Test o tang ModbusReader._run(): factory() raise (qua acquire()) ->
    status.error duoc set, status.online=False, _run() return SACH (khong
    crash ca thread, khong lot vao vong lap chinh, khong goi release() tren
    client chua bao gio duoc tao)."""
    reader = ModbusReader(_tcp_cfg(poll_ms=10), emit=Mock())

    def failing_factory():
        raise OSError("khong mo duoc cong")

    fake_shared = _SharedModbusClient(factory=failing_factory)

    with patch("node_agent.readers.modbus._get_shared_client", return_value=fake_shared):
        t = threading.Thread(target=reader._run, daemon=True)
        t.start()
        t.join(timeout=2)

    assert not t.is_alive()
    assert reader.status.online is False
    assert "khong mo duoc cong" in reader.status.error
    assert reader._client is None


# ----------------------------------------------------------------------
# 3c) _get_shared_client() - registry theo key

def test_get_shared_client_same_key_returns_same_object():
    key = ("tcp", "1.2.3.4", 502)
    factory = lambda: MagicMock()  # noqa: E731

    client_a = _get_shared_client(key, factory)
    client_b = _get_shared_client(key, factory)

    assert client_a is client_b


def test_get_shared_client_different_key_returns_different_object():
    factory = lambda: MagicMock()  # noqa: E731

    client_a = _get_shared_client(("tcp", "1.2.3.4", 502), factory)
    client_b = _get_shared_client(("tcp", "5.6.7.8", 502), factory)

    assert client_a is not client_b


def test_two_modbus_readers_same_rtu_port_share_one_client_no_exclusive_lock_conflict():
    """Regression truc tiep cho bug that tren OrangePi3B: 2 kenh Modbus RTU
    cung cam bien vat ly, cung cong serial (khac `address` thanh ghi) truoc
    day moi ModbusReader tu goi ModbusSerialClient(...) RIENG -> pymodbus mo
    voi exclusive=True, reader thu 2 nhan [Errno 11] Could not exclusively
    lock port. Fix: ca 2 reader CUNG key (conn_type, port, baud) phai dung
    CHUNG dung 1 _SharedModbusClient, va ModbusSerialClient CHI duoc khoi
    tao DUNG 1 LAN du co bao nhieu ModbusReader."""
    reader1 = ModbusReader(_rtu_cfg(address=10), emit=Mock())
    reader2 = ModbusReader(_rtu_cfg(address=20), emit=Mock())
    assert reader1._client_key == reader2._client_key  # cung key du khac address

    fake_client = MagicMock()
    with patch("node_agent.readers.modbus.ModbusSerialClient", return_value=fake_client) as mock_serial:
        client1 = _get_shared_client(reader1._client_key, reader1._client_factory)
        client1.acquire()
        client2 = _get_shared_client(reader2._client_key, reader2._client_factory)
        client2.acquire()

    assert client1 is client2  # cung 1 object _SharedModbusClient
    mock_serial.assert_called_once_with("/dev/ttyUSB1", baudrate=19200)  # CHI 1 instance duoc tao


# ----------------------------------------------------------------------
# 4) ModbusReader._read_once() - contract Approach B: tra ve list [(code,
#    value), ...] (1 phan tu khi KHONG dung "points", dung chinh reader.code
#    lam code cua phan tu do - xem docstring _read_once() trong modbus.py)

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
    assert value == [(reader.code, 100 * 2.0 + 5.0)]


def test_read_once_input_register_calls_read_input_registers():
    reader = _reader_with_fake_client({"register_type": "input", "data_type": "u16",
                                        "address": 20, "unit_id": 1})
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [42]
    reader._client.read_input_registers.return_value = rr

    value = reader._read_once()

    reader._client.read_input_registers.assert_called_once_with(20, count=1, device_id=1)
    assert value == [(reader.code, 42)]


def test_read_once_32bit_dtype_reads_two_registers():
    reader = _reader_with_fake_client({"register_type": "holding", "data_type": "u32", "address": 0})
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = _encode("u32", 70000)
    reader._client.read_holding_registers.return_value = rr

    value = reader._read_once()

    reader._client.read_holding_registers.assert_called_once_with(0, count=2, device_id=1)
    assert value == [(reader.code, 70000)]


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

    assert reader._read_once() == [(reader.code, 77)]


# ----------------------------------------------------------------------
# 5) ModbusReader._run() - vong lap thread that

def test_run_retries_connect_with_increasing_backoff():
    """3 lan ensure_connected() dau that bai (connect() tra False) roi thanh
    cong - self._stop.wait() phai duoc goi voi backoff TANG DAN (1.0 -> 2.0
    -> 4.0), khong duoc giu nguyen hang so hay giam. Dung _SharedModbusClient
    THAT (khong mock ensure_connected truc tiep) de test ca su ket hop giua
    logic backoff cua _run() va logic connect() that su cua wrapper."""
    reader = ModbusReader(_tcp_cfg(poll_ms=1000), emit=Mock())
    fake_underlying = MagicMock()
    fake_underlying.is_socket_open.return_value = False  # luon coi la chua mo
    attempts = {"n": 0}

    def fake_connect():
        attempts["n"] += 1
        if attempts["n"] < 4:
            return False
        reader._stop.set()  # thanh cong lan 4 - dung vong lap ngay, khong doc gi them
        return True

    fake_underlying.connect.side_effect = fake_connect
    shared = _SharedModbusClient(factory=lambda: fake_underlying)

    waits = []

    def fake_wait(timeout=None):
        waits.append(timeout)
        return False

    with patch("node_agent.readers.modbus._get_shared_client", return_value=shared), \
         patch.object(reader._stop, "wait", side_effect=fake_wait):
        reader._run()

    assert waits == [1.0, 2.0, 4.0]
    assert attempts["n"] == 4
    assert shared._refcount == 0  # release() trong finally cua _run() da chay dung 1 lan


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
    fake_underlying = _fake_underlying_client(connected=True)
    shared = _SharedModbusClient(factory=lambda: fake_underlying)
    waits = []

    def fake_wait(timeout=None):
        waits.append(timeout)
        if len(waits) >= 4:
            reader._stop.set()
        return False

    with patch("node_agent.readers.modbus._get_shared_client", return_value=shared), \
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
    fake_underlying = _fake_underlying_client(connected=True)
    shared = _SharedModbusClient(factory=lambda: fake_underlying)
    # Contract Approach B: _read_once() tra ve LIST [(code, value)], khong
    # con 1 float don - 1 phan tu duy nhat khi khong dung "points" (dung
    # chinh reader.code).
    single_point_values = [[(reader.code, v)] for v in (1.0, 2.0, 3.0, 4.0, 5.0)]

    with patch("node_agent.readers.modbus._get_shared_client", return_value=shared), \
         patch.object(reader, "_read_once", side_effect=single_point_values * 50):
        t = threading.Thread(target=reader._run, daemon=True)
        t.start()
        time.sleep(0.05)
        reader._stop.set()
        t.join(timeout=2)

    assert not t.is_alive()
    assert len(emitted) >= 1
    assert emitted[0] == ("ch1", 1.0, 0, True)
    assert reader._client is None  # finally cua _run() da release() va xoa tham chieu
    assert shared._refcount == 0


def test_run_emits_none_and_breaks_inner_loop_on_read_error():
    """_read_once() loi -> emit(code, None, None, 2, False) roi break khoi
    vong doc trong, quay lai vong ngoai goi lai ensure_connected() (KHONG
    dong shared client giua chung - khac ban truoc khi chia se client, vi
    client co the dang duoc reader KHAC dung chung cung luc)."""
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    reader = ModbusReader(_tcp_cfg(poll_ms=10), emit=fake_emit)
    fake_underlying = _fake_underlying_client(connected=True)
    shared = _SharedModbusClient(factory=lambda: fake_underlying)
    ensure_calls = {"n": 0}
    close_count_seen_during_loop = []
    real_ensure_connected = shared.ensure_connected

    def counting_ensure_connected():
        ensure_calls["n"] += 1
        close_count_seen_during_loop.append(fake_underlying.close.call_count)
        if ensure_calls["n"] >= 2:
            reader._stop.set()
        return real_ensure_connected()

    shared.ensure_connected = counting_ensure_connected

    with patch("node_agent.readers.modbus._get_shared_client", return_value=shared), \
         patch.object(reader, "_read_once", side_effect=IOError("loi doc modbus")), \
         patch.object(reader._stop, "wait", return_value=False):
        reader._run()

    assert ("ch1", None, 2, False) in emitted
    assert ensure_calls["n"] == 2  # vong ngoai lap lai (ensure_connected goi lai) sau loi doc
    # Client KHONG bi dong() giua vong doc (moi lan ensure_connected duoc goi,
    # close.call_count van la 0) - chi dong DUY NHAT o release() trong finally
    # cua _run() sau khi da dung han.
    assert close_count_seen_during_loop == [0, 0]
    fake_underlying.close.assert_called_once()


# ----------------------------------------------------------------------
# 6) ModbusReader.command("write", value)

def test_command_write_16bit_holding_register_success():
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="u16",
                                    address=15, unit_id=2), emit=Mock())
    reader._client = MagicMock()
    reader._client.write_register.return_value.isError.return_value = False

    result = reader.command("write", 123)

    reader._client.write_register.assert_called_once_with(15, 123, device_id=2)
    reader._client.write_registers.assert_not_called()
    assert result == {"ok": True, "status": "ok"}


def test_command_write_32bit_holding_register_calls_write_registers():
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="f32",
                                    address=20, unit_id=1), emit=Mock())
    reader._client = MagicMock()
    reader._client.write_registers.return_value.isError.return_value = False

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


def test_command_write_device_rejects_write_isError_response_returns_ok_false():
    """Regression cho finding python-reviewer 2026-09-25: thiet bi tu choi
    ghi (dia chi/ham khong hop le...) tra ve response BINH THUONG voi
    isError()==True - KHONG raise exception. Bo qua check nay se bao ok=True
    cho 1 lenh ghi thiet bi THAT SU tu choi, pha vo invariant dedup-theo-id
    o agent.py::_command_loop (lenh bi coi la xong va khong bao gio duoc thu
    lai du khong co gi thay doi tren thiet bi)."""
    reader = ModbusReader(_tcp_cfg(register_type="holding", data_type="u16"), emit=Mock())
    reader._client = MagicMock()
    reader._client.write_register.return_value.isError.return_value = True
    reader._client.write_register.return_value.__str__.return_value = "IllegalAddress"

    result = reader.command("write", 5)

    assert result["ok"] is False
    assert "IllegalAddress" in result["error"]


# ----------------------------------------------------------------------
# 7) Approach B - "points" (nhieu diem do gop trong 1 request Modbus) - xem
#    docstring dau file modbus.py va ModbusReader.__init__.

def test_read_once_multipoint_decodes_each_point_from_combined_read():
    """Regression truc tiep cho gia tri THAT do tren OrangePi3B (cam bien
    nhiet-do-am RS485 CHI tra loi dung khi doc gop address=0 count=2, hoi
    tung thanh ghi rieng se sai kieu response/timeout) - _read_once() phai
    goi DUNG 1 request doc gop (count=2) roi decode moi diem tu vi tri
    reg_offset rieng cua no trong khoi vua doc, KHONG duoc tach thanh N
    request rieng cho N diem (day la diem cot loi cua Huong B, giam so
    round-trip tren bus vat ly)."""
    cfg = _tcp_cfg(points=[
        {"code": "a", "reg_offset": 0, "data_type": "u16", "scale": 0.1, "offset": 0},
        {"code": "b", "reg_offset": 1, "data_type": "u16", "scale": 0.1, "offset": 0},
    ])
    reader = ModbusReader(cfg, emit=Mock())
    reader._client = MagicMock()
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [259, 588]  # gia tri that (nhiet do 25.9, do am 58.8)
    reader._client.read_holding_registers.return_value = rr

    values = reader._read_once()

    assert values == [("a", 25.9), ("b", 58.8)]
    reader._client.read_holding_registers.assert_called_once_with(0, count=2, device_id=1)


@pytest.mark.parametrize("points,expected_count", [
    ([{"code": "a", "reg_offset": 0, "data_type": "u16"}], 1),
    ([{"code": "a", "reg_offset": 0, "data_type": "u16"},
      {"code": "b", "reg_offset": 1, "data_type": "u16"}], 2),
    ([{"code": "a", "reg_offset": 0, "data_type": "u16"},
      {"code": "b", "reg_offset": 1, "data_type": "f32"}], 3),
    ([{"code": "a", "reg_offset": 0, "data_type": "u16"},
      {"code": "b", "reg_offset": 2, "data_type": "f32"}], 4),
])
def test_count_computed_from_max_reg_offset_plus_width(points, expected_count):
    """self._count phai la max(reg_offset+width) qua TAT CA point - du
    16-bit (width=1) hay 32-bit (width=2), va du diem rong nhat KHONG phai
    diem cuoi cung trong list."""
    reader = ModbusReader(_tcp_cfg(points=points), emit=Mock())

    assert reader._count == expected_count


def test_points_backward_compat_no_points_key_behaves_as_single_point():
    """KHONG co "points" trong cfg -> tuong thich nguoc 100% voi Phase 1:
    self._points co dung 1 phan tu, code khop code cua chinh cfg, reg_offset
    mac dinh 0 (doc dung tu self._address), va _read_once() van tra ve
    [(code, value)] y het hanh vi truoc Huong B."""
    cfg = _tcp_cfg(register_type="holding", data_type="u16", address=5, unit_id=2, scale=3.0, offset=1.0)
    reader = ModbusReader(cfg, emit=Mock())

    assert len(reader._points) == 1
    assert reader._points[0]["code"] == cfg["code"]
    assert reader._points[0]["reg_offset"] == 0

    reader._client = MagicMock()
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [10]
    reader._client.read_holding_registers.return_value = rr

    assert reader._read_once() == [(cfg["code"], 10 * 3.0 + 1.0)]


def test_run_emits_each_point_separately_on_success():
    """_run() chay THAT (thread that + join timeout, khong mock _read_once)
    voi cau hinh multi-point - moi point phai duoc emit() DUNG code/value
    rieng cua no, khong lan lon giua cac diem."""
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    cfg = _tcp_cfg(poll_ms=10, points=[
        {"code": "a", "reg_offset": 0, "data_type": "u16", "scale": 0.1, "offset": 0},
        {"code": "b", "reg_offset": 1, "data_type": "u16", "scale": 0.1, "offset": 0},
    ])
    reader = ModbusReader(cfg, emit=fake_emit)
    fake_underlying = _fake_underlying_client(connected=True)
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [259, 588]
    fake_underlying.read_holding_registers.return_value = rr
    shared = _SharedModbusClient(factory=lambda: fake_underlying)

    with patch("node_agent.readers.modbus._get_shared_client", return_value=shared):
        t = threading.Thread(target=reader._run, daemon=True)
        t.start()
        time.sleep(0.05)
        reader._stop.set()
        t.join(timeout=2)

    assert not t.is_alive()
    a_emits = [e for e in emitted if e[0] == "a"]
    b_emits = [e for e in emitted if e[0] == "b"]
    assert len(a_emits) >= 1
    assert len(a_emits) == len(b_emits)          # cung so lan doc thanh cong cho ca 2 diem
    assert all(v == 25.9 and q == 0 and ok is True for _, v, q, ok in a_emits)
    assert all(v == 58.8 and q == 0 and ok is True for _, v, q, ok in b_emits)


def test_run_emits_none_for_all_points_on_read_error():
    """Loi doc gop (exception) -> emit(code, None, None, 2, False) phai duoc
    goi cho TUNG point trong self._points, khong chi point dau."""
    emitted = []

    def fake_emit(code, value, raw, quality, ok):
        emitted.append((code, value, quality, ok))

    cfg = _tcp_cfg(poll_ms=10, points=[
        {"code": "a", "reg_offset": 0, "data_type": "u16"},
        {"code": "b", "reg_offset": 1, "data_type": "u16"},
    ])
    reader = ModbusReader(cfg, emit=fake_emit)
    fake_underlying = _fake_underlying_client(connected=True)
    shared = _SharedModbusClient(factory=lambda: fake_underlying)
    ensure_calls = {"n": 0}
    real_ensure_connected = shared.ensure_connected

    def counting_ensure_connected():
        ensure_calls["n"] += 1
        if ensure_calls["n"] >= 2:
            reader._stop.set()
        return real_ensure_connected()

    shared.ensure_connected = counting_ensure_connected

    with patch("node_agent.readers.modbus._get_shared_client", return_value=shared), \
         patch.object(reader, "_read_once", side_effect=IOError("loi doc modbus")), \
         patch.object(reader._stop, "wait", return_value=False):
        reader._run()

    assert ("a", None, 2, False) in emitted
    assert ("b", None, 2, False) in emitted


def test_run_survives_emit_raising_exception_sets_error_and_keeps_retrying():
    """Regression Critical (python-reviewer 2026-09-25): vong `for code, value
    in values: self.emit(...)` TRUOC DAY nam NGOAI try/except boc _read_once()
    - bat ky exception nao tu emit() se thoat thang khoi _run() (chi bi
    finally release() client, khong duoc bat), giet thread reader VINH VIEN
    va IM LANG (thread nay KHONG duoc Agent._guarded() bao ve, no la thread
    rieng cua ChannelReader). Fix: dua vong emit() vao TRONG cung try - loi
    tu emit() duoc xu ly y het loi doc (status.error, emit(None) bao loi cho
    tung point, backoff, roi VAN TIEP TUC vong lap ke tiep, KHONG chet)."""
    emitted = []
    raised = {"n": 0}
    error_seen = {"value": None}

    def fake_emit(code, value, raw, quality, ok):
        if ok and raised["n"] == 0:
            raised["n"] += 1
            raise RuntimeError("loi gia lap trong emit()")
        if not ok:
            # status.error da duoc set (boi except) NGAY TRUOC khi emit(...,
            # ok=False) duoc goi cho tung point - chup lai tai day vi vong lap
            # ngoai se RESET no ve None ngay khi ket noi lai thanh cong.
            error_seen["value"] = reader.status.error
        emitted.append((code, value, quality, ok))

    reader = ModbusReader(_tcp_cfg(poll_ms=10), emit=fake_emit)
    fake_underlying = _fake_underlying_client(connected=True)
    rr = MagicMock()
    rr.isError.return_value = False
    rr.registers = [77]
    fake_underlying.read_holding_registers.return_value = rr
    shared = _SharedModbusClient(factory=lambda: fake_underlying)
    wait_calls = {"n": 0}

    def fake_wait(timeout=None):
        # Khong sleep that (backoff toi thieu 1.0s) - chi dem lan goi va tu
        # dung sau du vong lap de xac nhan reader TIEP TUC hoat dong sau loi
        # emit(), khong can cho backoff that.
        wait_calls["n"] += 1
        if wait_calls["n"] >= 3:
            reader._stop.set()
        return False

    with patch("node_agent.readers.modbus._get_shared_client", return_value=shared), \
         patch.object(reader._stop, "wait", side_effect=fake_wait):
        t = threading.Thread(target=reader._run, daemon=True)
        t.start()
        t.join(timeout=2)

    assert not t.is_alive(), (
        "thread reader van dang chay/da chet ngoai kiem soat sau khi emit() "
        "raise - Critical finding (emit ngoai try) chua duoc fix dung")
    assert raised["n"] == 1                          # emit() thuc su da raise dung 1 lan
    assert error_seen["value"] is not None, (
        "status.error KHONG duoc set truoc khi emit(ok=False) - chung to "
        "exception tu emit() thoat thang khoi _run() ma KHONG duoc except nao bat")
    assert "loi gia lap trong emit()" in error_seen["value"]
    assert any(e[3] is False for e in emitted), "phai co it nhat 1 emit(..., ok=False) bao loi"
    assert any(e[3] is True for e in emitted), (
        "sau loi phai TIEP TUC vong lap va emit thanh cong lai duoc (thread khong chet)")


def test_command_write_targets_correct_point_via_channel_param():
    """command(..., channel="b") phai nham DUNG point "b" (reg_offset=1,
    scale/offset rieng), KHONG phai point dau tien "a"."""
    cfg = _tcp_cfg(register_type="holding", address=10, unit_id=1, points=[
        {"code": "a", "reg_offset": 0, "data_type": "u16", "scale": 1.0, "offset": 0.0},
        {"code": "b", "reg_offset": 1, "data_type": "u16", "scale": 2.0, "offset": 5.0},
    ])
    reader = ModbusReader(cfg, emit=Mock())
    reader._client = MagicMock()
    reader._client.write_register.return_value.isError.return_value = False

    result = reader.command("write", 25.0, channel="b")  # raw = (25-5)/2 = 10

    reader._client.write_register.assert_called_once_with(11, 10, device_id=1)  # address(10) + reg_offset(1)
    assert result == {"ok": True, "status": "ok"}


def test_command_write_unknown_channel_returns_error_not_default_point():
    """channel khong ton tai trong self._points -> tra loi, KHONG duoc am
    tham ghi vao point mac dinh (point dau tien)."""
    cfg = _tcp_cfg(points=[
        {"code": "a", "reg_offset": 0, "data_type": "u16"},
        {"code": "b", "reg_offset": 1, "data_type": "u16"},
    ])
    reader = ModbusReader(cfg, emit=Mock())
    reader._client = MagicMock()

    result = reader.command("write", 30.0, channel="khong_ton_tai")

    assert result["ok"] is False
    reader._client.write_register.assert_not_called()
    reader._client.write_registers.assert_not_called()


def test_command_write_no_channel_defaults_to_first_point_backward_compat():
    """Goi command() KHONG truyen channel (giong code cu goi truc tiep) ->
    tuong thich nguoc, dung point dau tien (self._points[0])."""
    cfg = _tcp_cfg(register_type="holding", address=10, unit_id=1, points=[
        {"code": "a", "reg_offset": 0, "data_type": "u16", "scale": 1.0, "offset": 0.0},
        {"code": "b", "reg_offset": 1, "data_type": "u16", "scale": 2.0, "offset": 5.0},
    ])
    reader = ModbusReader(cfg, emit=Mock())
    reader._client = MagicMock()
    reader._client.write_register.return_value.isError.return_value = False

    result = reader.command("write", 42)  # KHONG truyen channel

    reader._client.write_register.assert_called_once_with(10, 42, device_id=1)  # point dau (a): scale=1, offset=0
    assert result == {"ok": True, "status": "ok"}
