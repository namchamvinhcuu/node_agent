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

OUTPUT (2026-10-02, pcm-downlink-command): `"direction": "output"` bien kenh
thanh relay/den dieu khien tu Odoo - cung ngu nghia ESP32
components/gpio_out/gpio_out.c::gpio_out_execute(): on | off | write (value
!= 0 -> bat) | blink (period_ms >= 100); `ms` > 0 kem on/write-bat = bat roi
TU TAT sau ms; `ms` kem blink = dung nhay sau ms. `identify` (lenh cap node
ben ESP32) KHONG lam o day. Mac dinh direction=input - config cu khong doi.
Kenh output van emit trang thai hien tai (0/1) theo poll_ms de Odoo thay
lenh da co hieu luc. Mo lai device (reconnect) -> chan ve TAT (initial_value
=False), an toan hon giu trang thai khong ro."""
import logging
import threading

from gpiozero import DigitalInputDevice, DigitalOutputDevice

from .base import ChannelReader, duration_ms, onoff_level

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
        direction = str(cfg.get("direction", "input")).strip().lower()
        if direction not in ("input", "output"):
            raise ValueError("direction phai la input hoac output, khong phai %r" % direction)
        self._output = direction == "output"
        # command() chay tren thread _command_loop, device do _run() (reader
        # thread) tao/dong - khoa de command() khong dung device dang close().
        self._lock = threading.Lock()
        self._timer = None          # hen gio tu tat (lenh kem ms)
        self._gen = 0

    def _connect(self):
        if self._output:
            # invert o day = relay kich muc thap (active-low): value logic 1
            # (BAT) xuat muc 0 ra chan - gpiozero lo phan dao nay.
            device = DigitalOutputDevice(self._pin, active_high=not self._invert, initial_value=False)
        else:
            device = DigitalInputDevice(self._pin, pull_up=self._pull_up, bounce_time=self._bounce_s)
        with self._lock:
            self._device = device

    def _close(self):
        with self._lock:
            device, self._device = self._device, None
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if device:
            device.close()

    def _read_once(self) -> float:
        value = 1.0 if self._device.value else 0.0
        if self._output:
            return value          # da la gia tri logic (active_high xu ly invert)
        return (1.0 - value) if self._invert else value

    def command(self, cmd: str, value=None, channel: str = None, ms=None, period_ms=None, **opts) -> dict:
        if not self._output:
            return {"ok": False, "error": "kenh %s la GPIO input, khong dieu khien duoc" % self.code}
        try:
            ms = duration_ms(ms)
            period_ms = duration_ms(period_ms)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        level = True
        if cmd == "write":
            level = onoff_level(value)
            if level is None:
                return {"ok": False, "error": "write can value so (khac 0 = bat)"}
        elif cmd in ("on", "off"):
            level = cmd == "on"
        elif cmd != "blink":
            return {"ok": False, "error": "kenh GPIO output chi nhan: write | on | off | blink"}
        with self._lock:
            device = self._device
            if device is None:
                return {"ok": False, "error": "chua khoi tao duoc GPIO pin %s" % self._pin}
            if cmd == "blink":
                # Khop ESP32: <=0 -> 1000ms, duoi 100ms mat khong theo kip.
                period_s = max(100, period_ms or 1000) / 1000.0
                device.blink(on_time=period_s / 2, off_time=period_s / 2, n=None, background=True)
            elif level:
                device.on()             # on()/off() tu dung blink dang chay
            else:
                device.off()
            # Moi lenh moi huy hen gio cua lenh truoc (khop ESP32: lenh moi ghi
            # de expire_us) - CHI sau khi ghi chan thanh cong: ghi raise thi
            # hen gio tu tat cua lenh truoc van con, relay khong bi ket BAT.
            # _gen de timer cu da lo kich hoat (dang cho _lock) tu bo qua.
            self._gen += 1
            if self._timer:
                self._timer.cancel()
                self._timer = None
            # Hen gio chi co nghia khi BAT/nhay (khop ESP32): het ms -> TAT
            # dung luc do, ke ca giua chu ky nhay (khong lam tron so chu ky).
            if ms > 0 and level:
                self._timer = threading.Timer(ms / 1000.0, self._expire, args=(self._gen, device))
                self._timer.daemon = True
                self._timer.start()
        return {"ok": True, "status": "ok"}

    def _expire(self, gen, device):
        with self._lock:
            if gen != self._gen or self._device is not device:
                return              # da co lenh moi hon, hoac device da dong/mo lai
            self._timer = None
            try:
                device.off()
            except Exception:                                       # noqa: BLE001
                _logger.exception("kenh %s: tu tat sau ms that bai", self.code)

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
            self._close()
