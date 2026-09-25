# -*- coding: utf-8 -*-
"""Doc 1 kenh qua Modbus RTU (serial) hoac Modbus TCP - dung cho thiet bi cong
nghiep (bien tan, PLC, cam bien Modbus). Logic decode/encode gia tri (struct-
based, ho tro u16/i16/u32/i32/f32) copy tu
../edge_collector/edge_collector/drivers/modbus.py (da verify thuc te tren
he thong nay) - KHAC ve kien truc: edge_collector dung AsyncModbusClient
(FastAPI/asyncio), o day dung ModbusClient DONG BO (node_agent la stdlib
threading, moi kenh 1 thread rieng, khong asyncio cho business logic).

API sync client cua pymodbus 3.15.0 da verify thuc nghiem (khong doan theo
tri nho): read_holding_registers()/read_input_registers()/write_register()/
write_registers() deu dung tham so `device_id=` (KHONG phai `slave=` hay
`unit=` nhu ban cu cua pymodbus) - giong het API cua ban Async ma
edge_collector dang dung, dong nhat giua 2 project.

KHAC serial_ascii.py (mo cong that bai thi return vinh vien, khong tu retry)
- reader nay TU RETRY voi backoff khi mat ket noi/loi doc, vi thiet bi Modbus
mang (TCP) thuong gap gian doan tam thoi (PLC restart, mang chap chon) hon la
loi vinh vien nhu port serial khong ton tai.

CHIA SE 1 client giua nhieu ModbusReader cung port/unit vat ly (vd 1 cam
bien nhiet-am RS485 tra ve 2 thanh ghi -> khai bao 2 kenh cung
port+baud, khac address): pymodbus mo serial voi `exclusive=True` CO Y
(bao ve RTU half-duplex khoi 2 client ghi/doc dong thoi lam hong khung
tren cung bus vat ly - xac nhan qua source pymodbus 3.15.0, KHONG phai
bug thu vien) - N ModbusReader tu mo N client RIENG cung 1 port se lam
reader thu 2 tro di nhan EWOULDBLOCK (tranh chap khoa doc quyen cua he
dieu hanh, da tai hien bang socat + xac nhan bang deploy that tren
OrangePi3B, xem vault-debugger 2026-09-25). Fix: `_SharedModbusClient`
+ `_get_shared_client()` - registry theo key (conn_type, host/port, port/
baud), N reader cung key dung CHUNG 1 client, refcount dong khi reader
CUOI CUNG dung no dừng. `_io_lock` bat buoc rieng voi refcount: RTU la
half-duplex du co share client hay khong - 2 thread goi read/write dong
thoi van chong khung tren day vat ly that, GIL Python khong bao ve duoc
chuyen nay (day la gioi han giao thuc serial, khong phai gioi han ngon
ngu).

APPROACH B (2026-09-25, built right after Approach A was deployed for
real): 1 Modbus "source" can now have MULTIPLE points ("points" in
channels.json) read in a SINGLE combined request - needed because of a
REAL finding on OrangePi3B: a cheap RS485 temp+humidity sensor only
answers correctly when reading address=0 count=2 (temperature + humidity
together in one request); asking for a single register alone (e.g.
address=1) returns the WRONG response type (`ReadCoilsResponse` instead
of `ReadHoldingRegistersResponse`) or times out entirely - this class of
cheap firmware doesn't accept a sub-range read starting mid-block, only
the one fixed block from the base address. `points` is OPTIONAL - when
absent, behavior is 100% backward compatible with Phase 1 (treated as a
single point using this same cfg)."""
import logging
import struct
import threading

from pymodbus.client import ModbusSerialClient, ModbusTcpClient

from .base import ChannelReader

_logger = logging.getLogger("node.reader.modbus")

_BACKOFF_MIN_S = 1.0
_BACKOFF_MAX_S = 30.0
_MAX_CONSECUTIVE_ERRORS = 5

_registry_lock = threading.Lock()
_shared_clients: dict = {}


class _SharedModbusClient:
    """Boc 1 client pymodbus (Serial hoac TCP) dung CHUNG cho nhieu
    ModbusReader tro ve cung 1 ket noi vat ly. `_io_lock` serial hoa MOI
    lenh doc/ghi (khong chi luc connect) - RTU la half-duplex, 2 request
    dong thoi tren cung day vat ly se chong khung bat ke co share client
    Python object hay khong."""

    def __init__(self, factory):
        self._factory = factory
        self._client = None
        self._io_lock = threading.Lock()
        self._refcount = 0
        self._consecutive_errors = 0

    def acquire(self):
        # Tang refcount SAU khi factory() thanh cong - neu factory() raise
        # (ly thuyet, factory hien tai chi tao object khong lam I/O nen chua
        # co input hop le nao kich hoat duoc, van sua theo dung tinh than
        # review-rule da duyet) thi refcount KHONG bi lech vinh vien - xem
        # python-reviewer 2026-09-25 finding Minor.
        with _registry_lock:
            if self._client is None:
                self._client = self._factory()
            self._refcount += 1

    def release(self):
        # KHONG goi close() trong luc giu _registry_lock - phai serial hoa
        # voi cac lenh doc/ghi dang chay (dung _io_lock rieng) de tranh
        # close() chay song song voi 1 lenh I/O dang do dang tren CUNG client
        # (vd command() dang write_register() dung luc reader cuoi cung
        # dung/release()) - xem python-reviewer 2026-09-25 finding Major #1.
        with _registry_lock:
            self._refcount = max(0, self._refcount - 1)
            should_close = self._refcount == 0 and self._client is not None
        if should_close:
            with self._io_lock:
                if self._client is not None:
                    self._client.close()
                    self._client = None

    def ensure_connected(self):
        with self._io_lock:
            if self._client is None:
                raise ConnectionError("client da bi dong")
            if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                # pymodbus sync client (Serial/TCP) KHONG tu dong dong socket
                # that su khi transaction.count_until_disconnect am - da
                # verify THUC NGHIEM (socat + timeout lien tiep): is_socket_
                # open() van tra True mai mai du server/thiet bi khong bao
                # gio phan hoi (co che tu dong chi hoat dong cho client
                # ASYNC, khong ap dung cho sync client dang dung o day). Neu
                # khong tu dem loi va cuong che dong+mo lai, reader se "treo"
                # vinh vien o muc giao thuc (khac mat ket noi tang OS) toi
                # khi restart ca tien trinh - xem python-reviewer 2026-09-25
                # finding Major #2 (xac nhan dung nhu nghi van ban dau).
                self._client.close()
                self._consecutive_errors = 0
            if not self._client.is_socket_open() and not self._client.connect():
                raise ConnectionError("khong ket noi duoc")

    def _call(self, method_name, *args, **kwargs):
        with self._io_lock:
            try:
                result = getattr(self._client, method_name)(*args, **kwargs)
            except Exception:
                self._consecutive_errors += 1
                raise
            self._consecutive_errors = 0
            return result

    def read_holding_registers(self, *args, **kwargs):
        return self._call("read_holding_registers", *args, **kwargs)

    def read_input_registers(self, *args, **kwargs):
        return self._call("read_input_registers", *args, **kwargs)

    def write_register(self, *args, **kwargs):
        return self._call("write_register", *args, **kwargs)

    def write_registers(self, *args, **kwargs):
        return self._call("write_registers", *args, **kwargs)


def _get_shared_client(key, factory):
    with _registry_lock:
        shared = _shared_clients.get(key)
        if shared is None:
            shared = _SharedModbusClient(factory)
            _shared_clients[key] = shared
        return shared


def _decode(regs, dtype: str):
    dtype = (dtype or "u16").lower()
    if dtype in ("i16", "u16"):
        v = regs[0]
        return v - 0x10000 if dtype == "i16" and v >= 0x8000 else v
    raw = struct.pack(">HH", regs[0] & 0xFFFF, regs[1] & 0xFFFF)
    if dtype == "f32":
        return struct.unpack(">f", raw)[0]
    if dtype == "i32":
        return struct.unpack(">i", raw)[0]
    if dtype == "u32":
        return struct.unpack(">I", raw)[0]
    return regs[0]


def _reg_width(dtype: str) -> int:
    return 1 if (dtype or "u16").lower() in ("i16", "u16") else 2


def _parse_point(p: dict) -> dict:
    """1 point read as part of a combined multi-value read (Approach B) -
    e.g. an RS485 temp+humidity sensor only answers correctly when reading
    address=0 count=2 in a single request (confirmed on real hardware,
    OrangePi3B, 2026-09-25: asking for address=1 alone returns the wrong
    response type / times out). `reg_offset` is the position (in REGISTERS,
    not bytes) within the read block, relative to the source's `address`."""
    dtype = (p.get("data_type") or "u16").lower()
    return {
        "code": p["code"],
        "reg_offset": int(p.get("reg_offset", 0)),
        "data_type": dtype,
        "scale": float(p.get("scale", 1) or 1),
        "offset": float(p.get("offset", 0) or 0),
        "width": _reg_width(dtype),
    }


def _encode(dtype: str, value: float):
    # round() (KHONG phai int()/truncate) truoc khi ep kieu nguyen - sai so
    # dau phay dong khi tinh raw = (value - offset) / scale (command("write"))
    # se lam int() cat cut sai 1 don vi thanh ghi so voi gia tri Nam dinh ghi
    # that su - da verify thuc nghiem voi scale=0.1 (dung scale cua kenh
    # vfd_speed trong channels.example.json): 11101/4001 gia tri trong khoang
    # -2000..2000 bi lech 1 don vi neu dung int() - xem python-reviewer
    # 2026-09-25 (finding cung ton tai o edge_collector/drivers/modbus.py,
    # khong phai regression rieng cua file nay).
    dtype = (dtype or "u16").lower()
    if dtype in ("i16", "u16"):
        return [round(value) & 0xFFFF]
    if dtype == "f32":
        raw = struct.pack(">f", float(value))
    elif dtype == "i32":
        raw = struct.pack(">i", round(value))
    else:
        raw = struct.pack(">I", round(value) & 0xFFFFFFFF)
    hi, lo = struct.unpack(">HH", raw)
    return [hi, lo]


class ModbusReader(ChannelReader):
    mode = "modbus"

    def __init__(self, cfg, emit):
        super().__init__(cfg, emit)
        self._client = None
        self._unit = int(cfg.get("unit_id", 1) or 1)
        self._address = int(cfg.get("address", 0))
        self._register_type = cfg.get("register_type", "holding")
        # Approach B (2026-09-25): a Modbus source can now have MULTIPLE
        # points ("points" in channels.json), read together in a single
        # request - confirmed on real hardware (OrangePi3B): an RS485
        # temp+humidity sensor only answers correctly when reading
        # address=0 count=2 (temperature + humidity together), rejecting/
        # mis-answering a request for a single register alone (see
        # Plan/node-agent-multi-protocol-readers.md). No "points" -> 100%
        # backward compatible with the old (Phase 1) config: treated as a
        # single point using this same cfg (reg_offset=0 by default, so it
        # reads exactly from `address`, code matches self.code) - output
        # values are unchanged from before Approach B.
        points_cfg = cfg.get("points") or [cfg]
        self._points = [_parse_point(p) for p in points_cfg]
        self._count = max(p["reg_offset"] + p["width"] for p in self._points)
        # Ep kieu NGAY o __init__ (duoc agent.py::_build_readers() boc
        # try/except) thay vi doc lai tu self.cfg trong _run() - _run() chay
        # tren thread TRAN khong duoc Agent._guarded() bao ve, channels.json
        # sua tay ngoai UI co the ghi gia tri sai kieu (vd poll_ms dang chuoi)
        # se lam thread chet im lang vinh vien du status.online da la True -
        # bug tuong tu da fix o readers/gpio.py (python-reviewer 2026-09-25,
        # review-rule da duyet cho node_agent/readers/*.py), pre-existing tu
        # Phase 1, sua theo yeu cau Nam.
        self._poll_s = max(0.2, float(cfg.get("poll_ms", 1000)) / 1000.0)
        # Ep kieu host/port/baud NGAY o __init__ (cung ly do voi poll_ms o
        # tren) - _client_key/_client_factory duoc doc lai moi lan _run()
        # (vd sau khi reader restart) nhung khong duoc phep ep kieu LAI o do,
        # vi _run() chay tren thread tran khong duoc bao ve.
        if cfg.get("conn_type") == "tcp":
            host = cfg.get("host", "127.0.0.1")
            port = int(cfg.get("tcp_port", 502))
            self._client_key = ("tcp", host, port)
            self._client_factory = lambda: ModbusTcpClient(host, port=port)
        else:
            port = cfg.get("port", "/dev/ttyUSB0")
            baud = int(cfg.get("baud", 9600))
            self._client_key = ("rtu", port, baud)
            self._client_factory = lambda: ModbusSerialClient(port, baudrate=baud)

    def _read_once(self):
        """Read once, combining enough registers for ALL points
        (self._count) in a single request, then decode each point from its
        own position (`reg_offset`) within the block just read - returns a
        list of (code, value) for EVERY point (just 1 element when not
        using "points", preserving the old behavior). Only 1 request per
        loop iteration whether there's 1 or N points - fewer round-trips on
        the physical bus."""
        if self._register_type == "input":
            rr = self._client.read_input_registers(self._address, count=self._count, device_id=self._unit)
        else:
            rr = self._client.read_holding_registers(self._address, count=self._count, device_id=self._unit)
        if rr.isError():
            raise IOError(str(rr))
        results = []
        for p in self._points:
            regs = rr.registers[p["reg_offset"]:p["reg_offset"] + p["width"]]
            raw = _decode(regs, p["data_type"])
            results.append((p["code"], round(raw * p["scale"] + p["offset"], 6)))
        return results

    def _run(self):
        self._client = _get_shared_client(self._client_key, self._client_factory)
        try:
            self._client.acquire()
        except Exception as exc:                                   # noqa: BLE001
            self.status.online = False
            self.status.error = str(exc)[:200]
            _logger.warning("kenh %s: khong khoi tao duoc client modbus dung chung: %s",
                             self.code, exc)
            self._client = None
            return
        backoff = _BACKOFF_MIN_S
        try:
            while not self._stop.is_set():
                try:
                    self._client.ensure_connected()
                except Exception as exc:                           # noqa: BLE001
                    self.status.online = False
                    self.status.error = str(exc)[:200]
                    _logger.warning("kenh %s: khong ket noi duoc, thu lai sau %.1fs: %s",
                                     self.code, backoff, exc)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX_S)
                    continue
                self.status.online = True
                self.status.error = None
                while not self._stop.is_set():
                    try:
                        values = self._read_once()
                        for code, value in values:
                            self.emit(code, value, None, 0, True)
                    except Exception as exc:                        # noqa: BLE001
                        # KHONG reset backoff o day - thiet bi TON TAI nhung doc
                        # LUON loi (sai address/register_type/unit_id, hoac PLC
                        # tra exception response co dinh) van phai bi rate-limit
                        # y het nhanh connect-fail, neu khong se busy-loop
                        # connect+read hang tram nghin lan/giay - da verify thuc
                        # nghiem (python-reviewer 2026-09-25), rui ro DoS thiet
                        # bi cong nghiep that. KHONG dong client o day nua (khac
                        # ban truoc khi chia se) - client co the dang duoc N
                        # reader khac dung chung, dong se ngat ca cac reader do;
                        # ensure_connected() o vong lap ngoai se tu phat hien va
                        # reconnect neu that su mat ket noi (is_socket_open()).
                        # The emit loop above is INSIDE this try on purpose
                        # (python-reviewer 2026-09-25 Critical finding): any
                        # exception raised while emitting must be caught the
                        # same way as a read error - this reader thread is
                        # NOT protected by Agent._guarded(), so letting it
                        # escape would kill the thread silently forever.
                        self.status.error = str(exc)[:200]
                        _logger.warning("kenh %s: loi doc modbus, thu lai sau %.1fs: %s",
                                         self.code, backoff, exc)
                        for p in self._points:
                            self.emit(p["code"], None, None, 2, False)
                        self._stop.wait(backoff)
                        backoff = min(backoff * 2, _BACKOFF_MAX_S)
                        break
                    # CHI reset backoff sau khi doc THANH CONG that su - khong
                    # phai ngay sau connect (xem comment tren).
                    backoff = _BACKOFF_MIN_S
                    self._stop.wait(self._poll_s)
        finally:
            self._client.release()
            self._client = None

    def command(self, cmd: str, value=None, channel: str = None) -> dict:
        if cmd != "write" or value is None:
            return {"ok": False, "error": "modbus chi ho tro cmd=write kem value"}
        if self._register_type == "input":
            return {"ok": False, "error": "input register la read-only, khong ghi duoc"}
        # A source can have MULTIPLE points (Approach B) - `channel`
        # (passed down by agent.py from the real command's target channel
        # id) identifies the RIGHT point to write to (each point has its
        # own scale/offset/address). No `channel` given (e.g. old direct
        # calls, or a single-point source) -> defaults to the FIRST point,
        # preserving behavior from before Approach B.
        point = self._points[0]
        if channel is not None:
            point = next((p for p in self._points if p["code"] == channel), None)
            if point is None:
                return {"ok": False, "error": "kenh %s khong thuoc nguon modbus nay" % channel}
        if not self._client:
            return {"ok": False, "error": "chua ket noi toi thiet bi"}
        try:
            raw = (float(value) - point["offset"]) / point["scale"]
            regs = _encode(point["data_type"], raw)
            address = self._address + point["reg_offset"]
            if len(regs) == 1:
                resp = self._client.write_register(address, regs[0], device_id=self._unit)
            else:
                resp = self._client.write_registers(address, regs, device_id=self._unit)
            # A device rejecting the write (illegal address/function...)
            # comes back as a normal response with isError()==True - it does
            # NOT raise (verified against real pymodbus source, same as
            # _read_once() already has to check). Missing this check would
            # report ok=True for a write the device actually refused,
            # breaking the dedup-by-id invariant in agent.py::_command_loop
            # (the command is marked done and never retried even though
            # nothing changed on the device) - python-reviewer 2026-09-25.
            if resp.isError():
                return {"ok": False, "error": str(resp)}
            return {"ok": True, "status": "ok"}
        except Exception as exc:                                    # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
