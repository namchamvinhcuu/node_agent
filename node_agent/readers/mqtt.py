# -*- coding: utf-8 -*-
"""Doc 1 kenh qua MQTT (subscribe 1 topic co dinh) - dung cho gateway/cam bien
da co san firmware day MQTT (khac Modbus la node chu dong poll, o day broker
CHU DONG day gia tri moi ve bat cu luc nao - push-based).

KHAC voi ../edge_collector/edge_collector/drivers/mqtt.py (MqttDriver): ben do
la 1 SOURCE dung chung cho NHIEU kenh (subscribe topic_base + "/#", tu suy ma
kenh tu ten topic con), chay tren asyncio event loop (edge_collector la
FastAPI/asyncio). O day la 1 READER cho DUNG 1 kenh code co san trong
channels.json (giong sim/serial/modbus), subscribe DUNG 1 topic khai bao san,
KHONG asyncio - dung callback thread rieng cua paho-mqtt (`loop_start()`),
con thread cua chinh reader nay chi ngoi cho `self._stop` roi don dep khi
duoc yeu cau dung (khop kien truc stdlib-threading, moi kenh 1 thread, cua
node_agent).

Payload: so tho (`float(payload)`) mac dinh, hoac neu khai bao `json_key`
trong channels.json thi parse JSON va lay dung 1 key phang do (KHONG ho tro
nested path kieu "a.b.c" - YAGNI, chua co use-case nao can nested, them sau
neu Nam yeu cau). Loi parse (JSON hong, thieu key, khong phai so) KHONG lam
crash reader - chi emit quality=2 (loi) va ghi status.error, giu cho cac
message hop le tiep theo van duoc xu ly binh thuong.

paho-mqtt 1.6.1 tu dong reconnect (backoff exponential mac dinh 1s->120s qua
`reconnect_delay_set`, gia tri mac dinh cua thu vien) trong network loop khi
dung `connect_async()` + `loop_start()` - KHONG can tu viet retry loop nhu
modbus.py (thu vien da lo, khac Modbus sync client khong tu retry)."""
import json
import logging

import paho.mqtt.client as mqtt

from .base import ChannelReader

_logger = logging.getLogger("node.reader.mqtt")


def _parse_payload(raw: bytes, json_key: str):
    """Tra ve (value, error) - dung 1 trong 2, khong bao gio ca 2 cung co."""
    text = raw.decode(errors="replace")
    if json_key:
        try:
            data = json.loads(text)
        except ValueError as exc:
            return None, "JSON khong hop le: %s" % exc
        if not isinstance(data, dict) or json_key not in data:
            return None, "thieu key '%s' trong payload JSON" % json_key
        try:
            return float(data[json_key]), None
        except (TypeError, ValueError):
            return None, "key '%s' khong phai so" % json_key
    try:
        return float(text), None
    except ValueError:
        return None, "payload khong phai so: %r" % text[:80]


class MqttReader(ChannelReader):
    mode = "mqtt"

    def __init__(self, cfg, emit):
        super().__init__(cfg, emit)
        self._client = None
        self._topic = cfg["topic"]
        self._json_key = cfg.get("json_key") or ""
        self._cmd_topic = cfg.get("cmd_topic") or (self._topic + "/cmd")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.status.online = True
            self.status.error = None
            client.subscribe(self._topic)
        else:
            self.status.online = False
            self.status.error = "mqtt connect rc=%s" % rc
            _logger.warning("kenh %s: mqtt connect rc=%s", self.code, rc)

    def _on_disconnect(self, client, userdata, rc):
        self.status.online = False
        if rc != 0:
            self.status.error = "mqtt bi ngat rc=%s" % rc
            _logger.warning("kenh %s: mqtt bi ngat rc=%s, cho tu reconnect", self.code, rc)

    def _on_connect_fail(self, client, userdata):
        # paho tu retry vo han trong network loop rieng (loop_start()) va CHI
        # log o muc MQTT_LOG_DEBUG qua logger noi bo cua no (client.py, verify
        # thuc te tren pymqtt 1.6.1 dang cai) - khong dang ky callback nay thi
        # host/port sai (vd typo channels.json) se im lang vinh vien, status.
        # error khong bao gio duoc set vi _on_connect/_on_disconnect chua tung
        # fire (ket noi chua thanh cong lan nao) - xem python-reviewer
        # 2026-09-25 finding Major #2.
        self.status.online = False
        self.status.error = "khong ket noi duoc toi broker mqtt"
        _logger.warning("kenh %s: ket noi mqtt that bai, dang tu retry", self.code)

    def _on_message(self, client, userdata, msg):
        value, err = _parse_payload(msg.payload, self._json_key)
        if err is not None:
            self.status.error = err[:200]
            _logger.warning("kenh %s: loi parse payload mqtt: %s", self.code, err)
            self.emit(self.code, None, None, 2, False)
            return
        self.emit(self.code, round(value, 6), None, 0, True)

    def _run(self):
        # TOAN BO phan chuan bi (ep kieu tu channels.json, connect_async) nam
        # trong 1 try/except duy nhat - ChannelReader.start() (base.py) spawn
        # thread TRAN, KHONG duoc Agent._guarded() bao ve (wrapper do chi ap
        # cho 6 loop cap Agent) - channels.json co the bi sua tay ngoai UI
        # (agent.py comment dong 65-71) nen 1 gia tri sai kieu (vd `port` la
        # chuoi rong) khong duoc bat se lam thread nay CHET IM LANG VINH VIEN,
        # khong log/khong status.error - xem python-reviewer 2026-09-25
        # finding Major #1 (Phase 1/Modbus da lam dung, Phase 2 nay tai pham).
        client = mqtt.Client()
        try:
            username = self.cfg.get("username")
            if username:
                client.username_pw_set(username, self.cfg.get("password") or "")
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_connect_fail = self._on_connect_fail
            client.on_message = self._on_message
            client.enable_logger(_logger)
            host = self.cfg.get("host", "127.0.0.1")
            port = int(self.cfg.get("port", 1883))
            client.connect_async(host, port, keepalive=30)
        except Exception as exc:                                   # noqa: BLE001
            self.status.online = False
            self.status.error = str(exc)[:200]
            _logger.warning("kenh %s: khong the ket noi mqtt: %s", self.code, exc)
            return
        self._client = client
        client.loop_start()
        self._stop.wait()
        client.loop_stop()
        client.disconnect()
        self._client = None

    def command(self, cmd: str, value=None) -> dict:
        # Kiem CA self._client CA status.online TRUOC khi publish, roi kiem
        # lai info.rc SAU khi publish - publish() cua paho KHONG raise khi mat
        # ket noi (QoS 0 mac dinh tra MQTT_ERR_NO_CONN va DROP message vinh
        # vien, khong requeue) nen `try/except` khong bat duoc case nay. Bao
        # sai `ok: True` se lam agent.py::_command_loop nho nham id la "da
        # thuc thi thanh cong" (dedup-by-id) va KHONG BAO GIO retry lenh that
        # su bi mat - xem python-reviewer 2026-09-25 finding Critical, verify
        # truc tiep tren source paho-mqtt 1.6.1 dang cai (_send_publish()).
        if not self._client or not self.status.online:
            return {"ok": False, "error": "chua ket noi toi broker"}
        payload = value if value is not None else cmd
        try:
            info = self._client.publish(self._cmd_topic, str(payload))
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return {"ok": False, "error": "publish that bai (rc=%s)" % info.rc}
            return {"ok": True, "status": "ok"}
        except Exception as exc:                                    # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
