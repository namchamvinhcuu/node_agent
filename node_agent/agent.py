# -*- coding: utf-8 -*-
"""NodeAgent - vong doi cua MOT thiet bi (Pi/PC bridge) noi thang vao edge
qua hop dong node_api.py. Moi kenh mot reader thread (sim hoac serial that);
mot hang doi chung gom du lieu lai roi gui theo lo, giong dung cach edge lam
voi Odoo (bid/seq co dinh + tang dan, luu SQLite de song sot qua restart).
"""
import collections
import logging
import queue
import random
import signal
import threading
import time
import uuid

from .config import settings
from .edge_client import EdgeClient
from .mqtt_uplink import MqttUplink, verify
from .readers.gpio import GpioReader
from .readers.modbus import ModbusReader
from .readers.mqtt import MqttReader
from .readers.serial_ascii import SerialReader
from .readers.sim import SimReader
from .settings_api import router as settings_router
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("node.agent")

READER_CLASSES = {"sim": SimReader, "serial": SerialReader, "modbus": ModbusReader,
                   "mqtt": MqttReader, "gpio": GpioReader}

# Khop nodes/esp32/components/uplink/uplink.c::backoff_sleep() - gui that bai
# lien tuc (edge sap hoac dang restart) ma cu doi 2s co dinh se don dap edge
# ngay luc no yeu nhat. Tang gap doi toi tran, cong jitter de nhieu node
# khong dong loat thu lai cung 1 nhip.
_SENDER_BACKOFF_MIN_S = 2.0
_SENDER_BACKOFF_MAX_S = 30.0

# So khoa lenh da thuc thi THANH CONG duoc nho de dedup (xem _command_loop).
# 1 id duy nhat (truoc 2026-10-02) khong du: A,B,A (edge gui lai A sau khi B
# da chay) se chay A 2 lan.
_CMD_DEDUP_WINDOW = 32
# Lenh ky co "ts" (unix giay, gio edge) lech qua nguong nay -> "stale" (chong
# replay lenh cu bat duoc tren broker). Thong nhat voi edge-collector-58.
_CMD_MAX_SKEW_S = 120


def _cmd_key(cmd: dict):
    # request_id (uuid Odoo, edge forward nguyen) on dinh xuyen qua ca lan edge
    # tao lai lenh voi id moi - uu tien no; lenh cu khong co thi dung id.
    rid = cmd.get("request_id")
    if isinstance(rid, str) and rid:
        return ("rid", rid)
    return ("id", cmd["id"])


def _echo_rid(cmd: dict):
    # Echo trong ack (ACK duoc node KY) - lenh gia mao khong duoc nho node ky
    # chuoi dai tuy y; uuid hex Odoo chi 32 ky tu (gioi han khop edge inbound_api).
    rid = cmd.get("request_id")
    return rid if isinstance(rid, str) and 0 < len(rid) <= 64 else None


def _invalid_reason(cmd) -> str:
    """'' neu lenh dung hinh dang toi thieu, nguoc lai ly do tu choi."""
    if not isinstance(cmd, dict):
        return "lenh khong phai JSON object"
    if not isinstance(cmd.get("id"), int) or isinstance(cmd.get("id"), bool):
        return "thieu/sai id"
    for field in ("channel", "cmd"):
        if not isinstance(cmd.get(field), str) or not cmd[field]:
            return "thieu/sai %s" % field
    return ""


class NodeAgent:
    def __init__(self):
        self.store = Store(settings.sqlite_path)
        self.client = EdgeClient(self.store)
        # None = giu HTTP (legacy, NODE_MQTT_UPLINK_HOST bo trong). Chi start()
        # sau khi _hello_loop bao thanh cong lan dau - xem mqtt_uplink.py.
        self.mqtt = MqttUplink(self.store) if settings.mqtt_uplink_enabled else None
        self._mqtt_started = threading.Event()
        # "Kick" thay vi cho het timer co dinh - khop pattern ESP32
        # (xTaskNotifyGive/ulTaskNotifyTake trong mqtt_link.c: FLUSH_MS chi la
        # fallback, du lieu moi den la day di publish NGAY). Ap dung cho CA HTTP
        # lan MQTT (dung chung _flush_loop/_sender_loop) - giam do tre hien thi
        # realtime, khong doi hanh vi batch (van gom moi thu tich luy trong
        # khoang thoi gian rat ngan giua luc kick va luc doc queue).
        self._flush_kick = threading.Event()
        self._sender_kick = threading.Event()
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
        self._flush_kick.set()

    def _build_readers(self):
        for cfg in settings.load_channels():
            mode = cfg.get("mode", "sim")
            cls = READER_CLASSES.get(mode)
            if not cls:
                _logger.warning("kenh %s: mode '%s' khong ho tro", cfg.get("code"), mode)
                continue
            try:
                reader = cls(cfg, self._emit)
            except Exception:                                       # noqa: BLE001
                # settings_api.py validate pattern/tham so TRUOC khi ghi qua
                # web UI, nhung channels.json van co the bi sua tay ngoai UI
                # (SSH, edit truc tiep) - 1 kenh cau hinh sai (vd regex loi
                # neu bo qua validate) KHONG duoc phep keo sap toan bo node,
                # vi __init__ chay dong bo o day (ngoai _guarded()) - loi TRUOC
                # day se crash ca process ngay luc khoi dong, ke ca cac kenh
                # khac dang hoat dong binh thuong - xem python-reviewer 2026-09-25.
                _logger.exception("kenh %s: khoi tao that bai, bo qua kenh nay", cfg.get("code"))
                continue
            # Approach B (ModbusReader "points"): 1 reader can represent
            # MULTIPLE channel codes at once (1 physical source, several
            # points read together in a single request) - map ALL of those
            # codes to the SAME reader instance, so _command_loop routes
            # commands to the right source and start()/stop() (using set()
            # to dedupe by identity) doesn't spawn a duplicate thread for
            # the same reader.
            codes = [p["code"] for p in cfg["points"]] if cfg.get("points") else [cfg["code"]]
            for code in codes:
                self._readers[code] = reader

    # ------------------------------------------------------------------
    def start(self):
        self._build_readers()
        # set() dedupes by object identity (ChannelReader doesn't override
        # __eq__/__hash__) - needed because an Approach B reader can appear
        # MULTIPLE TIMES in self._readers.values() (once per channel code
        # it represents); starting it twice would spawn 2 independent
        # threads for the SAME reader object (sharing self._stop but
        # calling _run() twice concurrently - unnecessary trouble).
        for r in set(self._readers.values()):
            r.start()
        loops = [self._hello_loop, self._flush_loop, self._sender_loop,
                 self._heartbeat_loop, self._command_loop, self._setup_server_loop]
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
        # _flush_loop/_sender_loop gio cho _flush_kick/_sender_kick (khac
        # self._stop) - phai tu danh thuc rieng, khong thi phai doi het not
        # submit_interval_s/1.0s con lai moi quay lai kiem duoc self._stop.is_set()
        # (python-reviewer 2026-09-29 bat finding nay).
        self._flush_kick.set()
        self._sender_kick.set()
        for r in set(self._readers.values()):
            r.stop()
        if self.mqtt and self._mqtt_started.is_set():
            self.mqtt.stop()
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
            elif self.mqtt and not self._mqtt_started.is_set():
                # Chi mo MQTT SAU KHI hello 200 - edge phai cache api_key
                # TRUOC thi moi verify duoc chu ky cua message MQTT dau tien.
                self.mqtt.start()
                self._mqtt_started.set()
            elif self.mqtt and not self.mqtt.sig_cmd and self.client.api_key:
                # Duong provision pho bien: node len TRUOC, Odoo tao device SAU
                # -> phien MQTT da connect luc chua co key (sig_cmd=False). Khong
                # khai lai thi ca phien do (co the nhieu ngay) nhan lenh KHONG ky.
                self.mqtt.announce_sig_cmd()
            self._stop.wait(settings.hello_interval_s)

    def _flush_loop(self):
        while not self._stop.is_set():
            # submit_interval_s la TRAN CHO (khong con reading nao thi van
            # flush dinh ky nhu cu); _flush_kick lam _emit() danh thuc NGAY
            # khi co reading moi, khong con phai doi het tran timer roi moi
            # kiem tra queue - giam do tre hien thi realtime cho ca HTTP lan
            # MQTT (khop pattern kick cua ESP32, xem ghi chu __init__).
            self._flush_kick.wait(settings.submit_interval_s)
            self._flush_kick.clear()
            items = []
            while True:
                try:
                    items.append(self._pending.get_nowait())
                except queue.Empty:
                    break
            if items:
                seq = self.store.next_seq()
                self.store.outbox_push(self._boot_id, seq, {"items": items})
                self._sender_kick.set()

    def _sender_loop(self):
        backoff_s = 0.0
        while not self._stop.is_set():
            row = self.store.outbox_oldest()
            if not row:
                self._sender_kick.wait(1.0)
                self._sender_kick.clear()
                continue
            if self.mqtt:
                ok = self.mqtt.measurements(row["payload"]["items"], row["bid"], row["seq"])
                err = "khong nhan PUBACK" if not ok else None
            else:
                res = self.client.measurements(row["payload"]["items"], row["bid"], row["seq"])
                ok, err = res.get("ok"), res.get("error")
            if ok:
                self.store.outbox_delete(row["id"])
                backoff_s = 0.0
            else:
                _logger.info("gui measurements that bai: %s", err)
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
        # lenh that su. Nho cua so _CMD_DEDUP_WINDOW khoa (request_id, khong co
        # thi id) thay vi 1 id. Cua so nam trong RAM, khai bao NGOAI vong lap
        # nen song sot qua moi lenh; lenh hong KHONG duoc lam crash loop (crash
        # -> _guarded() restart -> mat cua so dedup).
        done_keys: "collections.deque" = collections.deque(maxlen=_CMD_DEDUP_WINDOW)
        while not self._stop.is_set():
            if self.mqtt:
                # queue.get(timeout=...) da tu cho toi da command_poll_interval_s,
                # KHONG can them _stop.wait() rieng o nhanh nay.
                cmd = self.mqtt.next_command(settings.command_poll_interval_s)
            else:
                cmd = self.client.next_command().get("command")
            if cmd:
                reason = _invalid_reason(cmd)
                if reason:
                    cmd_id = cmd.get("id") if isinstance(cmd, dict) else None
                    _logger.warning("bo qua lenh khong hop le (%s): %r", reason, cmd)
                    if isinstance(cmd_id, int) and not isinstance(cmd_id, bool):
                        self._ack_command(cmd_id, False, reason, _echo_rid(cmd))
                    continue
                cmd_id = cmd["id"]
                rid = _echo_rid(cmd)
                reject = self._auth_reject_reason(cmd)
                if reject:
                    _logger.warning("tu choi lenh %s: %s", cmd_id, reject)
                    self._ack_command(cmd_id, False, reject, rid)
                    continue
                key = _cmd_key(cmd)
                if key in done_keys:
                    _logger.info("lenh %s da thuc thi roi, chi ack lai", cmd_id)
                    self._ack_command(cmd_id, True, "", rid)
                    continue
                reader = self._readers.get(cmd["channel"])
                try:
                    result = (reader.command(cmd["cmd"], cmd.get("value"), channel=cmd["channel"]) if reader
                              else {"ok": False, "error": "khong co kenh %s tren node nay" % cmd["channel"]})
                except Exception as exc:                            # noqa: BLE001
                    _logger.exception("lenh %s: reader.command() raise", cmd_id)
                    result = {"ok": False, "error": str(exc)[:200]}
                ok = result.get("ok", False)
                if ok:
                    done_keys.append(key)
                self._ack_command(cmd_id, ok, result.get("error") or "", rid)
                continue          # kiem tra ngay lenh ke tiep, khong cho
            if not self.mqtt:
                self._stop.wait(settings.command_poll_interval_s)

    def _auth_reject_reason(self, cmd: dict) -> str:
        """'' = cho phep chay. Bat buoc ky khi phien MQTT hien tai DA khai
        "sig_cmd": true (edge LUON ky khi thay co nay); con lai (MQTT chua co
        key luc connect, hoac HTTP poll khong khai duoc capability) -> edge
        khong ky, chi verify NEU lenh co kem sig."""
        required = bool(self.mqtt and self.mqtt.sig_cmd)
        if not required and "sig" not in cmd:
            return ""
        if not verify(self.client.api_key, cmd):
            return "sig_invalid"
        ts = cmd.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return "stale"
        if abs(time.time() + self.client.clock_offset_s - ts) > _CMD_MAX_SKEW_S:
            return "stale"
        return ""

    def _ack_command(self, cmd_id: int, ok: bool, detail: str, request_id: str = None):
        # Chi truyen request_id khi co - giu nguyen dang goi cu cho lenh khong
        # co request_id (edge ghep ack theo id + serial, request_id chi de echo).
        kw = {"request_id": request_id} if request_id else {}
        if self.mqtt:
            self.mqtt.ack_command(cmd_id, ok, detail, **kw)
        else:
            self.client.ack_command(cmd_id, ok, detail, **kw)

    def _setup_server_loop(self):
        # uvicorn tu quan ly asyncio loop rieng trong thread nay - phan con
        # lai cua node_agent van la stdlib threading thuan, KHONG dung chung
        # event loop. Watcher thread set should_exit khi self._stop bat, de
        # server.run() (blocking) tra ve thay vi cho SIGTERM/join timeout 5s.
        import uvicorn
        from fastapi import FastAPI

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(settings_router)
        server = uvicorn.Server(uvicorn.Config(
            app, host="0.0.0.0", port=settings.setup_port, log_level="warning"))

        def _watch_stop():
            self._stop.wait()
            server.should_exit = True

        threading.Thread(target=_watch_stop, daemon=True).start()
        server.run()
