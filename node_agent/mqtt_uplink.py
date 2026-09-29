# -*- coding: utf-8 -*-
"""Duong MQTT len edge (thay HTTP measurements + command) - mirror
nodes/esp32/components/mqtt_link/mqtt_link.c: topic fms/<serial>/{meas,status,cmd,cmdack}.

CHI duoc start() SAU KHI HTTP /hello da thanh cong it nhat 1 lan (agent.py::
_hello_loop goi start()) - edge_collector cache api_key qua /hello TRUOC, MQTT
publish qua som se bi tu choi vi edge chua co key de verify chu ky (thong nhat
voi session edge-collector-f3, 2026-09-29).

LWT retained tren topic status ({"online": false}, KHONG ky - luc broker phat
LWT thi client da mat ket noi, khong the ky dong duoc) de edge phat hien node
"chet" ngay khi mat TCP, nhanh hon heartbeat polling nhieu (khop pattern ESP32).
Sau khi connect thanh cong, tu publish {"online": true, "cmd": true} (retain)
de bao caps "nghe lenh qua MQTT" cho manager.queue_command ben edge chon dung
duong publish truc tiep thay vi roi ve poll-queue.

Ky HMAC-SHA256 bang api_key da hoc qua /hello cho MOI message CO THE ky dong
luc dang chay (meas/status-online/cmdack) - ESP32 dung chung 1 broker
username/password cho MOI thiet bi (khong co auth per-device, xem
components/mqtt_link/mqtt_link.c), node_agent (Pi/PC manh hon ve tinh toan)
siet chat them lop nay ma khong can dung PKI/mTLS moi.

Ack theo PUBACK (khop ESP32 "ack theo PUBACK chu khong theo enqueue"): dung
thang MQTTMessageInfo.wait_for_publish() co san cua paho (race-free bang
threading.Condition noi tai thu vien - da doc source venv_linux/.../paho/mqtt/
client.py xac nhan message duoc track vao _out_messages TRUOC khi goi
_send_publish(), nen khong co khoang ho giua "gui xong" va "dang theo doi ack"
nhu tu viet lai bang dict+Event rieng se bi - review 2026-09-29 bat finding nay).

publish() CHI duoc goi tren thread RIENG (goi tu agent.py::_sender_loop/
_command_loop qua measurements()/ack_command(), KHONG phai tren network thread
cua paho) VI wait_for_publish() block cho toi PUBACK - goi no ngay trong
on_connect/on_message (chay tren network thread cua paho) se tu khoa network
thread, khong con ai xu ly duoc chinh goi PUBACK dang cho (deadlock, cung
review 2026-09-29 bat o _on_connect). _on_connect vi vay dung wait_ack=False
(fire-and-forget, khong cho ack) cho message "online" luc connect; on_message
chi bo lenh vao queue roi tra ve NGAY, khong tu ack tai cho."""
import hashlib
import hmac
import json
import logging
import queue

import paho.mqtt.client as mqtt

from .config import settings
from .store import Store

_logger = logging.getLogger("node.mqtt_uplink")
_ACK_TIMEOUT_S = 5.0


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign(api_key: str, payload: dict) -> str:
    """HMAC-SHA256 tren canonical JSON cua payload (KHONG kem field 'sig').
    Ben verify (edge_collector) phai tach 'sig' ra khoi payload roi tinh lai
    tren PHAN CON LAI truoc khi so sanh."""
    return hmac.new(api_key.encode(), _canonical(payload), hashlib.sha256).hexdigest()


class MqttUplink:
    def __init__(self, store: Store):
        self.store = store
        self._topic_meas = "fms/%s/meas" % settings.serial
        self._topic_status = "fms/%s/status" % settings.serial
        self._topic_cmd = "fms/%s/cmd" % settings.serial
        self._topic_cmd_ack = "fms/%s/cmdack" % settings.serial
        self._cmd_queue: "queue.Queue" = queue.Queue()
        self._cli = mqtt.Client(client_id=settings.serial, clean_session=True)
        if settings.mqtt_uplink_username:
            self._cli.username_pw_set(settings.mqtt_uplink_username, settings.mqtt_uplink_password)
        self._cli.will_set(self._topic_status, json.dumps({"online": False}), qos=1, retain=True)
        self._cli.on_connect = self._on_connect
        self._cli.on_message = self._on_message
        self._cli.reconnect_delay_set(min_delay=1, max_delay=120)

    @property
    def api_key(self):
        return self.store.kv_get("api_key")

    def start(self):
        self._cli.connect_async(settings.mqtt_uplink_host, settings.mqtt_uplink_port, keepalive=30)
        self._cli.loop_start()

    def stop(self):
        self._cli.loop_stop()
        self._cli.disconnect()

    # ------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            _logger.info("mqtt uplink connect that bai rc=%s", rc)
            return
        client.subscribe(self._topic_cmd, qos=1)
        # wait_ack=False: dang chay TREN network thread cua paho, cho PUBACK
        # (wait_for_publish) tai day se tu khoa chinh no - xem docstring module.
        self._publish(self._topic_status, {"online": True, "cmd": True}, retain=True, wait_ack=False)
        _logger.info("mqtt uplink da ket noi %s:%s", settings.mqtt_uplink_host, settings.mqtt_uplink_port)

    def _on_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode())
        except ValueError:
            _logger.warning("lenh MQTT khong phai JSON hop le")
            return
        self._cmd_queue.put_nowait(cmd)

    # ------------------------------------------------------------------
    def _publish(self, topic: str, payload: dict, retain: bool = False, wait_ack: bool = True) -> bool:
        key = self.api_key
        body = dict(payload)
        if key:
            body["sig"] = sign(key, payload)
        info = self._cli.publish(topic, _canonical(body), qos=1, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False          # chua ket noi - khong co PUBACK nao se toi, dung cho
        if not wait_ack:
            return True
        info.wait_for_publish(timeout=_ACK_TIMEOUT_S)
        return info.is_published()

    def measurements(self, items: list, bid: str, seq: int) -> bool:
        return self._publish(self._topic_meas, {"items": items, "bid": bid, "seq": seq})

    def ack_command(self, cmd_id: int, ok: bool, detail: str = "") -> bool:
        return self._publish(self._topic_cmd_ack, {"id": cmd_id, "ok": ok, "detail": detail})

    def next_command(self, timeout: float):
        try:
            return self._cmd_queue.get(timeout=timeout)
        except queue.Empty:
            return None
