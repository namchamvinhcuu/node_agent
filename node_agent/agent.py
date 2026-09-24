# -*- coding: utf-8 -*-
"""NodeAgent - vong doi cua MOT thiet bi (Pi/PC bridge) noi thang vao edge
qua hop dong node_api.py. Moi kenh mot reader thread (sim hoac serial that);
mot hang doi chung gom du lieu lai roi gui theo lo, giong dung cach edge lam
voi Odoo (bid/seq co dinh + tang dan, luu SQLite de song sot qua restart).
"""
import logging
import queue
import random
import signal
import threading
import time
import uuid

from .config import settings
from .edge_client import EdgeClient
from .readers.serial_ascii import SerialReader
from .readers.sim import SimReader
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("node.agent")

READER_CLASSES = {"sim": SimReader, "serial": SerialReader}

# Khop nodes/esp32/components/uplink/uplink.c::backoff_sleep() - gui that bai
# lien tuc (edge sap hoac dang restart) ma cu doi 2s co dinh se don dap edge
# ngay luc no yeu nhat. Tang gap doi toi tran, cong jitter de nhieu node
# khong dong loat thu lai cung 1 nhip.
_SENDER_BACKOFF_MIN_S = 2.0
_SENDER_BACKOFF_MAX_S = 30.0


class NodeAgent:
    def __init__(self):
        self.store = Store(settings.sqlite_path)
        self.client = EdgeClient(self.store)
        self._pending: "queue.Queue" = queue.Queue()
        self._readers: dict = {}
        self._stop = threading.Event()
        self._threads: list = []
        self._boot_id = self.store.kv_get("boot_id")
        if not self._boot_id:
            self._boot_id = uuid.uuid4().hex
            self.store.kv_set("boot_id", self._boot_id)

    # ------------------------------------------------------------------
    def _emit(self, ch, v, s, q, stable):
        self._pending.put_nowait({"ch": ch, "v": v, "s": s, "q": int(q or 0), "stable": stable,
                                  "ts": int(time.time() * 1000)})

    def _build_readers(self):
        for cfg in settings.load_channels():
            mode = cfg.get("mode", "sim")
            cls = READER_CLASSES.get(mode)
            if not cls:
                _logger.warning("kenh %s: mode '%s' khong ho tro", cfg.get("code"), mode)
                continue
            self._readers[cfg["code"]] = cls(cfg, self._emit)

    # ------------------------------------------------------------------
    def start(self):
        self._build_readers()
        for r in self._readers.values():
            r.start()
        loops = [self._hello_loop, self._flush_loop, self._sender_loop,
                 self._heartbeat_loop, self._command_loop]
        for fn in loops:
            t = threading.Thread(target=self._guarded, args=(fn,), name=fn.__name__, daemon=True)
            t.start()
            self._threads.append(t)
        _logger.info("node %s da khoi dong voi %d kenh", settings.serial, len(self._readers))

    def _guarded(self, fn):
        while not self._stop.is_set():
            try:
                fn()
                return
            except Exception:                                       # noqa: BLE001
                _logger.exception("loop %s crash - restart sau 2s", fn.__name__)
                self._stop.wait(2.0)

    def stop(self):
        self._stop.set()
        for r in self._readers.values():
            r.stop()
        for t in self._threads:
            t.join(timeout=5)

    def run_forever(self):
        self.start()
        signal.signal(signal.SIGINT, lambda *a: self._stop.set())
        try:
            signal.signal(signal.SIGTERM, lambda *a: self._stop.set())
        except (ValueError, AttributeError):
            pass          # Windows: SIGTERM khong luon dung duoc trong tien trinh con
        while not self._stop.is_set():
            self._stop.wait(1.0)
        _logger.info("dang dung...")
        self.stop()

    # ------------------------------------------------------------------
    def _hello_loop(self):
        while not self._stop.is_set():
            res = self.client.hello()
            if not res.get("ok"):
                _logger.info("hello that bai: %s", res.get("error"))
            self._stop.wait(settings.hello_interval_s)

    def _flush_loop(self):
        while not self._stop.is_set():
            self._stop.wait(settings.submit_interval_s)
            items = []
            while True:
                try:
                    items.append(self._pending.get_nowait())
                except queue.Empty:
                    break
            if items:
                seq = self.store.next_seq()
                self.store.outbox_push(self._boot_id, seq, {"items": items})

    def _sender_loop(self):
        backoff_s = 0.0
        while not self._stop.is_set():
            row = self.store.outbox_oldest()
            if not row:
                self._stop.wait(1.0)
                continue
            res = self.client.measurements(row["payload"]["items"], row["bid"], row["seq"])
            if res.get("ok"):
                self.store.outbox_delete(row["id"])
                backoff_s = 0.0
            else:
                _logger.info("gui measurements that bai: %s", res.get("error"))
                backoff_s = min(_SENDER_BACKOFF_MAX_S,
                                backoff_s * 2 if backoff_s else _SENDER_BACKOFF_MIN_S)
                self._stop.wait(backoff_s + backoff_s * 0.2 * random.random())

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            self.client.heartbeat({
                "fw": "node_agent/0.1.0",
                "config_version": self.store.kv_get("config_version", 0),
            })
            self._stop.wait(settings.heartbeat_interval_s)

    def _command_loop(self):
        # Khop nodes/esp32/components/mqtt_link/mqtt_link.c::handle_command()
        # - poll co the tra lai dung 1 lenh (edge chua nhan duoc ack, hoac
        # request truoc bi mat giua duong) sau khi no DA thuc thi thanh cong.
        # Chi ack lai, KHONG thuc thi lai: mot lan bam nut khong duoc bien
        # thanh hai lan dao relay. Loi thi KHONG nho lai id - cho phep retry
        # lenh that su.
        last_ok_cmd_id = None
        while not self._stop.is_set():
            res = self.client.next_command()
            cmd = res.get("command")
            if cmd:
                cmd_id = cmd["id"]
                if cmd_id == last_ok_cmd_id:
                    _logger.info("lenh %s da thuc thi roi, chi ack lai", cmd_id)
                    self.client.ack_command(cmd_id, True, "")
                    continue
                reader = self._readers.get(cmd["channel"])
                result = (reader.command(cmd["cmd"], cmd.get("value")) if reader
                          else {"ok": False, "error": "khong co kenh %s tren node nay" % cmd["channel"]})
                ok = result.get("ok", False)
                if ok:
                    last_ok_cmd_id = cmd_id
                self.client.ack_command(cmd_id, ok, result.get("error") or "")
                continue          # kiem tra ngay lenh ke tiep, khong cho
            self._stop.wait(settings.command_poll_interval_s)
