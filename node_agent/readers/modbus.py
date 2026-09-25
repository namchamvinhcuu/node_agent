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
loi vinh vien nhu port serial khong ton tai."""
import logging
import struct

from pymodbus.client import ModbusSerialClient, ModbusTcpClient

from .base import ChannelReader

_logger = logging.getLogger("node.reader.modbus")

_BACKOFF_MIN_S = 1.0
_BACKOFF_MAX_S = 30.0


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
        self._dtype = (cfg.get("data_type") or "u16").lower()
        self._address = int(cfg.get("address", 0))
        self._register_type = cfg.get("register_type", "holding")
        self._scale = float(cfg.get("scale", 1) or 1)
        self._offset = float(cfg.get("offset", 0) or 0)

    def _connect(self):
        if self.cfg.get("conn_type") == "tcp":
            host = self.cfg.get("host", "127.0.0.1")
            port = int(self.cfg.get("tcp_port", 502))
            self._client = ModbusTcpClient(host, port=port)
        else:
            port = self.cfg.get("port", "/dev/ttyUSB0")
            baud = int(self.cfg.get("baud", 9600))
            self._client = ModbusSerialClient(port, baudrate=baud)
        if not self._client.connect():
            raise ConnectionError("khong ket noi duoc %s" % self.cfg.get(
                "host" if self.cfg.get("conn_type") == "tcp" else "port"))

    def _read_once(self):
        count = 1 if self._dtype in ("i16", "u16") else 2
        if self._register_type == "input":
            rr = self._client.read_input_registers(self._address, count=count, device_id=self._unit)
        else:
            rr = self._client.read_holding_registers(self._address, count=count, device_id=self._unit)
        if rr.isError():
            raise IOError(str(rr))
        raw = _decode(rr.registers, self._dtype)
        return raw * self._scale + self._offset

    def _run(self):
        poll_ms = self.cfg.get("poll_ms", 1000)
        backoff = _BACKOFF_MIN_S
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception as exc:                               # noqa: BLE001
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
                    value = self._read_once()
                    self.emit(self.code, round(value, 6), None, 0, True)
                except Exception as exc:                            # noqa: BLE001
                    # KHONG reset backoff o day - thiet bi TON TAI nhung doc
                    # LUON loi (sai address/register_type/unit_id, hoac PLC
                    # tra exception response co dinh) van phai bi rate-limit
                    # y het nhanh connect-fail, neu khong se busy-loop
                    # connect+read hang tram nghin lan/giay - da verify thuc
                    # nghiem (python-reviewer 2026-09-25), rui ro DoS thiet
                    # bi cong nghiep that.
                    self.status.error = str(exc)[:200]
                    _logger.warning("kenh %s: loi doc modbus, thu lai sau %.1fs: %s",
                                     self.code, backoff, exc)
                    self.emit(self.code, None, None, 2, False)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX_S)
                    break
                # CHI reset backoff sau khi doc THANH CONG that su - khong
                # phai ngay sau connect (xem comment tren).
                backoff = _BACKOFF_MIN_S
                self._stop.wait(max(0.2, poll_ms / 1000.0))
            if self._client:
                self._client.close()
                self._client = None

    def command(self, cmd: str, value=None) -> dict:
        if cmd != "write" or value is None:
            return {"ok": False, "error": "modbus chi ho tro cmd=write kem value"}
        if self._register_type == "input":
            return {"ok": False, "error": "input register la read-only, khong ghi duoc"}
        if not self._client:
            return {"ok": False, "error": "chua ket noi toi thiet bi"}
        try:
            raw = (float(value) - self._offset) / self._scale
            regs = _encode(self._dtype, raw)
            if len(regs) == 1:
                self._client.write_register(self._address, regs[0], device_id=self._unit)
            else:
                self._client.write_registers(self._address, regs, device_id=self._unit)
            return {"ok": True, "status": "ok"}
        except Exception as exc:                                    # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
