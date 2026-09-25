# -*- coding: utf-8 -*-
"""Doc digital input tren 1 chan GPIO cua CHINH thiet bi (Pi that) - dung
`gpiozero` (thay vi `RPi.GPIO` tho) vi co san `gpiozero.pins.mock.MockFactory`
cho phep TEST DUOC logic ma KHONG can Pi that (chi doi `Device.pin_factory`
tai runtime, khong can bien moi truong dac biet nao).

Da verify thuc nghiem tren may dev x86 (KHONG co Pi that, khong doan theo
memory): `import gpiozero` o MODULE-LEVEL AN TOAN - khong crash, khong
warning. Loi CHI xay ra khi THAT SU khoi tao mot Device (vd
`DigitalInputDevice(pin)`) vi khong tim duoc pin factory that (lgpio/
rpigpio/pigpio/native deu khong ton tai tren x86, raise `BadPinFactory`).
Vi vay reader nay import binh thuong nhu Modbus/MQTT (KHONG can lazy-import
nhu Plan ban dau du doan), chi bat loi trong `_connect()`/`_run()` giong
het pattern retry cua `modbus.py` (thiet bi chua san sang -> retry voi
backoff, khong phai loi import).

Pham vi Phase 3/3 cua ke hoach multi-protocol readers: DIGITAL INPUT ONLY
(analog va GPIO output/actuator NGOAI SCOPE - Nam chua yeu cau, se lam
rieng neu can). Poll-based (giong sim/modbus, dung `poll_ms`) - KHONG dung
callback `when_activated`/`when_deactivated` cua gpiozero de giu kien truc
don gian nhat quan, tranh lap lai lop bug threading-callback da gap o MQTT
(Phase 2: status ghi tu thread khac reader thread).

Reader nay CHI DOC (khong ho tro `command()` ghi) - dung default cua
`ChannelReader.command()` (base.py), khong override."""
import logging

from gpiozero import DigitalInputDevice

from .base import ChannelReader

_logger = logging.getLogger("node.reader.gpio")

_BACKOFF_MIN_S = 1.0
_BACKOFF_MAX_S = 30.0


def _cfg_bool(cfg: dict, key: str, default: bool) -> bool:
    # channels.json co the bi sua tay ngoai UI (bo qua validate cua
    # settings_api.py) - `bool("false")` == True (chuoi khong rong), se AM
    # THAM dao nguoc y dinh cau hinh neu ai do go `"pull_up": "false"` thay
    # vi JSON boolean `false`. Ep ve chuoi roi so sanh tuong minh de "false"/
    # "False" van duoc hieu dung - xem python-reviewer 2026-09-25 finding
    # Minor (Phase 3/GPIO).
    raw = cfg.get(key, default)
    return str(raw).strip().lower() in ("true", "1")


class GpioReader(ChannelReader):
    mode = "gpio"

    def __init__(self, cfg, emit):
        super().__init__(cfg, emit)
        self._device = None
        self._pin = cfg["pin"]
        self._pull_up = _cfg_bool(cfg, "pull_up", False)
        self._invert = _cfg_bool(cfg, "invert", False)
        bounce_ms = float(cfg.get("bounce_ms", 0) or 0)
        self._bounce_s = (bounce_ms / 1000.0) or None
        # Ep kieu + validate NGAY o __init__ (duoc agent.py::_build_readers()
        # boc try/except) thay vi trong _run() - _run() chay tren thread
        # TRAN khong duoc Agent._guarded() bao ve, 1 gia tri sai kieu (vd
        # channels.json sua tay ghi "poll_ms": "200" dang chuoi) se lam
        # `poll_ms / 1000.0` raise TypeError KHONG duoc bat, thread chet im
        # lang VINH VIEN trong khi status.online da tro thanh True tu truoc
        # do - te hon ca finding Major da fix o mqtt.py (Phase 2), vi status
        # bao SAI la kenh van online du du lieu da ngung chay - xem
        # python-reviewer 2026-09-25 finding Major (Phase 3/GPIO). Ap dung
        # dung review-rule (a) da duoc Nam duyet.
        self._poll_s = max(0.05, float(cfg.get("poll_ms", 200)) / 1000.0)

    def _connect(self):
        self._device = DigitalInputDevice(
            self._pin, pull_up=self._pull_up, bounce_time=self._bounce_s)

    def _read_once(self) -> float:
        value = 1.0 if self._device.value else 0.0
        return (1.0 - value) if self._invert else value

    def _run(self):
        backoff = _BACKOFF_MIN_S
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception as exc:                               # noqa: BLE001
                self.status.online = False
                self.status.error = str(exc)[:200]
                _logger.warning("kenh %s: khong khoi tao duoc GPIO pin %s, thu lai sau %.1fs: %s",
                                 self.code, self._pin, backoff, exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
                continue
            self.status.online = True
            self.status.error = None
            while not self._stop.is_set():
                try:
                    value = self._read_once()
                except Exception as exc:                           # noqa: BLE001
                    # KHONG reset backoff o day - khop dung pattern modbus.py
                    # (finding python-reviewer 2026-09-25, review-rule da
                    # duyet): thiet bi TON TAI nhung doc LUON loi van phai bi
                    # rate-limit y het nhanh connect-fail, tranh busy-loop.
                    self.status.error = str(exc)[:200]
                    _logger.warning("kenh %s: loi doc GPIO: %s", self.code, exc)
                    self.emit(self.code, None, None, 2, False)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX_S)
                    break
                self.emit(self.code, value, None, 0, True)
                backoff = _BACKOFF_MIN_S
                self._stop.wait(self._poll_s)
            if self._device:
                self._device.close()
                self._device = None
