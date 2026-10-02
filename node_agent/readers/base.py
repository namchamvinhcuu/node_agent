# -*- coding: utf-8 -*-
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

EmitCb = Callable[[str, Optional[float], Optional[str], int, Optional[bool]], None]

_MS_MAX = 2 ** 31 - 1      # khop ESP32 mqtt_link.c cmd_i32 (kep INT32_MAX)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def onoff_level(value) -> Optional[bool]:
    """Muc BAT/TAT cho cmd=write tren kenh relay (GPIO output, Modbus coil) -
    khop ESP32 gpio_out.c: value so != 0 -> bat, None -> TAT. Kieu khac
    (chuoi "1", true, list...) -> None = tu choi; ESP32 coi la TAT nhung tu
    choi an toan hon de doan y."""
    if value is None:
        return False
    if not _is_number(value):
        return None
    return value != 0


def duration_ms(v) -> int:
    """ms/period_ms cua lenh: None/<=0 -> 0 (khop cmd_i32 cua ESP32), kep
    INT32_MAX (gia tri qua lon lam thread blink cua gpiozero chet OverflowError
    trong khi lenh da bao ok). Khong phai so -> ValueError."""
    if v is None:
        return 0
    if not _is_number(v) or v != v:          # v != v: NaN
        raise ValueError("ms/period_ms phai la so")
    return int(min(max(v, 0), _MS_MAX))


@dataclass
class ReaderStatus:
    online: bool = False
    error: Optional[str] = None


class ChannelReader:
    mode = "base"

    def __init__(self, cfg: dict, emit: EmitCb):
        self.cfg = cfg
        self.code = cfg["code"]
        self.emit = emit
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status = ReaderStatus()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reader-%s" % self.code, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        raise NotImplementedError

    def command(self, cmd: str, value=None, channel: str = None, **opts) -> dict:
        # `channel` (the id of the channel actually being controlled) only
        # matters for a reader that represents MULTIPLE channels at once
        # (e.g. ModbusReader Approach B: 1 physical source with several
        # "points") - one-channel-one-device readers (sim/serial/mqtt/gpio)
        # ignore this param and always use self.code.
        # `opts` = field phu cua lenh (ms, period_ms - agent.py chi truyen khi
        # lenh co) - reader nao khong dung thi bo qua.
        return {"ok": False, "error": "kenh %s khong ho tro lenh" % self.code}
