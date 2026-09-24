# -*- coding: utf-8 -*-
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

EmitCb = Callable[[str, Optional[float], Optional[str], int, Optional[bool]], None]


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

    def command(self, cmd: str, value=None) -> dict:
        return {"ok": False, "error": "kenh %s khong ho tro lenh" % self.code}
