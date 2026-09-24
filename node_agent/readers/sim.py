# -*- coding: utf-8 -*-
import math
import random
import time

from .base import ChannelReader


class SimReader(ChannelReader):
    """Song gia (walk) - dung khi chua co cam bien that, kiem tra toan bo
    duong ong node -> edge -> Odoo."""
    mode = "sim"

    def _run(self):
        t0 = time.time()
        center = self.cfg.get("center", 50.0)
        spread = self.cfg.get("spread", 1.0)
        poll_ms = self.cfg.get("poll_ms", 1000)
        self.status.online = True
        while not self._stop.is_set():
            v = center + spread * math.sin(time.time() - t0) + random.uniform(-spread * 0.2, spread * 0.2)
            self.emit(self.code, round(v, 4), None, 0, True)
            self._stop.wait(max(0.1, poll_ms / 1000.0))

    def command(self, cmd: str, value=None) -> dict:
        return {"ok": True, "status": "ok"}
