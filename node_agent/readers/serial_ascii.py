# -*- coding: utf-8 -*-
"""Doc mot cam bien noi tiep ASCII (can dien tu, caliper...) cam thang vao
node. Cau hinh nam trong channels.json cua CHINH node nay (khong keo tu Odoo -
node duoc coi la 'cam' theo dung nghia PCM, firmware giu cau hinh cua no)."""
import logging
import re

import serial as pyserial

from .base import ChannelReader

_logger = logging.getLogger("node.reader.serial")


class SerialReader(ChannelReader):
    mode = "serial"

    def __init__(self, cfg, emit):
        super().__init__(cfg, emit)
        self._pattern = re.compile(cfg.get("pattern") or r"(.+)")
        self._term = (cfg.get("terminator") or "\r\n").encode()
        self._ser = None

    def _run(self):
        if not self.cfg.get("port"):
            self.status.error = "channels.json: kenh '%s' (mode=serial) thieu truong 'port'" % self.code
            _logger.warning(self.status.error)
            return
        parity_map = {"none": pyserial.PARITY_NONE, "even": pyserial.PARITY_EVEN,
                      "odd": pyserial.PARITY_ODD}
        try:
            self._ser = pyserial.Serial(
                port=self.cfg["port"], baudrate=self.cfg.get("baud", 9600),
                bytesize=self.cfg.get("data_bits", 8), stopbits=self.cfg.get("stop_bits", 1),
                parity=parity_map.get(self.cfg.get("parity", "none"), pyserial.PARITY_NONE),
                timeout=1.0,
            )
            self.status.online = True
        except Exception as exc:                                    # noqa: BLE001
            self.status.error = str(exc)[:200]
            _logger.warning("khong mo duoc cong %s: %s", self.cfg.get("port"), exc)
            return

        buf = b""
        while not self._stop.is_set():
            try:
                b = self._ser.read(1)
            except Exception as exc:                                # noqa: BLE001
                self.status.error = str(exc)[:200]
                self._stop.wait(1.0)
                continue
            if not b:
                continue
            buf += b
            if not buf.endswith(self._term):
                continue
            line, buf = buf[:-len(self._term)].decode(errors="replace"), b""
            self._parse_and_emit(line)
        if self._ser:
            self._ser.close()

    def _parse_and_emit(self, line: str):
        m = self._pattern.match(line.strip())
        if not m:
            return
        try:
            # can dien tu hay dem khoang trang giua dau +/- va so (o dinh dang
            # do rong co dinh) - bo trang trong roi moi doi float, vi "+   18.13"
            # lam float() nem ValueError.
            raw_val = re.sub(r"\s+", "", m.group(self.cfg.get("value_group", 1)))
            value = float(raw_val)
        except (IndexError, ValueError, TypeError):
            return
        stable = True
        sg, ok = self.cfg.get("stable_group"), self.cfg.get("stable_ok")
        if sg and ok:
            try:
                stable = m.group(sg).strip() == ok.strip()
            except IndexError:
                stable = True
        self.emit(self.code, value, None, 0, stable)

    def command(self, cmd: str, value=None, channel: str = None) -> dict:
        raw = {"zero": self.cfg.get("cmd_zero"), "tare": self.cfg.get("cmd_tare"),
               "read": self.cfg.get("cmd_read")}.get(cmd)
        if not raw or not self._ser:
            return {"ok": False, "error": "lenh '%s' khong duoc khai bao hoac cong chua mo" % cmd}
        try:
            self._ser.write(raw.encode())
            return {"ok": True, "status": "ok"}
        except Exception as exc:                                    # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
