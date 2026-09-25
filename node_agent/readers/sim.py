# -*- coding: utf-8 -*-
import math
import random
import time

from .base import ChannelReader


class SimReader(ChannelReader):
    """Song gia (walk) - dung khi chua co cam bien that, kiem tra toan bo
    duong ong node -> edge -> Odoo."""
    mode = "sim"

    def __init__(self, cfg, emit):
        super().__init__(cfg, emit)
        # Ep kieu NGAY o __init__ (duoc agent.py::_build_readers() boc
        # try/except) thay vi doc lai tu self.cfg trong _run() - _run() chay
        # tren thread TRAN khong duoc Agent._guarded() bao ve, channels.json
        # sua tay ngoai UI co the ghi gia tri sai kieu (vd poll_ms dang chuoi)
        # se lam thread chet im lang vinh vien du status.online da la True -
        # cung lop bug da fix o readers/gpio.py (python-reviewer 2026-09-25,
        # review-rule da duyet cho node_agent/readers/*.py).
        self._center = float(cfg.get("center", 50.0))
        self._spread = float(cfg.get("spread", 1.0))
        self._poll_s = max(0.1, float(cfg.get("poll_ms", 1000)) / 1000.0)

    def _run(self):
        t0 = time.time()
        self.status.online = True
        while not self._stop.is_set():
            v = (self._center + self._spread * math.sin(time.time() - t0)
                 + random.uniform(-self._spread * 0.2, self._spread * 0.2))
            self.emit(self.code, round(v, 4), None, 0, True)
            self._stop.wait(self._poll_s)

    def command(self, cmd: str, value=None, channel: str = None) -> dict:
        return {"ok": True, "status": "ok"}
