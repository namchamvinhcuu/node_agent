# -*- coding: utf-8 -*-
"""Test node_agent/mqtt_uplink.py (MqttUplink + sign()) - dung FAKE
mqtt.Client (KHONG mo socket/broker that) de kiem PUBACK-gated publish
(qua MQTTMessageInfo.wait_for_publish() THAT cua paho, xem docstring module),
wait_ack=False (fire-and-forget, dung o _on_connect de tranh tu khoa network
thread), LWT/status connect-time, va chu ky HMAC order-independent.

_FakeMqttClient.publish() tra ve mqtt.MQTTMessageInfo THAT (khong phai
SimpleNamespace) vi _publish() goi thang info.wait_for_publish()/
info.is_published() - can object that co _condition/_set_as_published() de
mo phong PUBACK arrive BAT DONG BO (Timer thread, giong network thread cua
paho that goi _set_as_published() sau khi nhan PUBACK tu broker)."""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import paho.mqtt.client as mqtt

import node_agent.config as config_module
import node_agent.mqtt_uplink as mqtt_uplink_module
from node_agent.mqtt_uplink import MqttUplink, sign


class _FakeMqttClient:
    """Thay real paho.mqtt.client.Client. publish() tra ve MQTTMessageInfo
    THAT cua paho; PUBACK duoc mo phong ARRIVE BAT DONG BO (Timer thread) qua
    info._set_as_published() - dung auto_ack=False de mo phong "khong bao gio
    nhan PUBACK" (wait_for_publish() timeout), publish_rc != MQTT_ERR_SUCCESS
    de mo phong "chua ket noi"."""

    def __init__(self):
        self.published = []       # list[(topic, payload_str_or_bytes, qos, retain)]
        self.subscribed = []
        self.will = None
        self.username = None
        self.password = None
        self.on_connect = None
        self.on_message = None
        self._next_mid = 1
        self.publish_rc = mqtt.MQTT_ERR_SUCCESS
        self.auto_ack = True

    def username_pw_set(self, username, password=None):
        self.username, self.password = username, password

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        pass

    def connect_async(self, host, port=1883, keepalive=60):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload=None, qos=0, retain=False):
        mid = self._next_mid
        self._next_mid += 1
        self.published.append((topic, payload, qos, retain))
        info = mqtt.MQTTMessageInfo(mid)
        info.rc = self.publish_rc
        if self.publish_rc == mqtt.MQTT_ERR_SUCCESS and self.auto_ack:
            threading.Timer(0.01, info._set_as_published).start()
        return info


def _make_uplink(monkeypatch, serial="NODE-TEST-01", api_key="secretkey123"):  # secret-allow (test fixture)
    monkeypatch.setattr(config_module.settings, "serial", serial)
    monkeypatch.setattr(config_module.settings, "mqtt_uplink_username", "")
    fake_client = _FakeMqttClient()
    monkeypatch.setattr(mqtt_uplink_module.mqtt, "Client", lambda *a, **kw: fake_client)
    store = Mock()
    store.kv_get.return_value = api_key
    uplink = MqttUplink(store)
    return uplink, fake_client


# ----------------------------------------------------------------------
# 1) sign() - HMAC-SHA256 tren canonical JSON, order-independent

def test_sign_is_order_independent():
    p1 = {"a": 1, "b": 2, "id": 42}
    p2 = {"id": 42, "b": 2, "a": 1}

    assert sign("key123", p1) == sign("key123", p2)


def test_sign_changes_when_a_field_changes():
    base = {"a": 1, "b": 2}
    changed = {"a": 1, "b": 3}

    assert sign("key123", base) != sign("key123", changed)


def test_sign_changes_with_different_key():
    payload = {"a": 1}

    assert sign("key-one", payload) != sign("key-two", payload)


# ----------------------------------------------------------------------
# 2) _publish() - cho PUBACK (wait_for_publish) truoc khi coi la thanh cong

def test_publish_waits_for_puback_and_returns_true(monkeypatch):
    uplink, fake = _make_uplink(monkeypatch)

    ok = uplink._publish(uplink._topic_meas, {"items": [], "bid": "b1", "seq": 1})

    assert ok is True
    topic, payload, qos, retain = fake.published[0]
    assert topic == uplink._topic_meas
    assert qos == 1
    body = json.loads(payload)
    assert body["bid"] == "b1" and body["seq"] == 1
    assert "sig" in body


def test_publish_signature_matches_sign_of_unsigned_payload(monkeypatch):
    uplink, fake = _make_uplink(monkeypatch, api_key="mykey")

    uplink._publish(uplink._topic_meas, {"items": [1, 2], "bid": "b1", "seq": 5})

    body = json.loads(fake.published[0][1])
    sig = body.pop("sig")
    assert sig == sign("mykey", body), (
        "chu ky phai tinh tren payload GOC (khong kem 'sig') - dung thuat "
        "toan ben verify (edge_collector) se dung lai")


def test_publish_omits_sig_when_no_api_key_learned_yet(monkeypatch):
    """Truoc khi /hello lan dau thanh cong, store chua co api_key - message
    van duoc gui (khong block) nhung KHONG co field 'sig' (khong the ky)."""
    uplink, fake = _make_uplink(monkeypatch, api_key=None)

    uplink._publish(uplink._topic_meas, {"items": []})

    body = json.loads(fake.published[0][1])
    assert "sig" not in body


def test_publish_times_out_when_no_puback(monkeypatch):
    uplink, fake = _make_uplink(monkeypatch)
    fake.auto_ack = False
    monkeypatch.setattr(mqtt_uplink_module, "_ACK_TIMEOUT_S", 0.05)

    ok = uplink._publish(uplink._topic_meas, {"items": []})

    assert ok is False


def test_publish_returns_false_immediately_when_not_connected(monkeypatch):
    """rc != MQTT_ERR_SUCCESS (chua ket noi) phai tra ve NGAY - khong duoc
    goi wait_for_publish()/cho _ACK_TIMEOUT_S vi se KHONG BAO GIO co PUBACK
    toi (chua co ket noi thi khong co gi de gui)."""
    uplink, fake = _make_uplink(monkeypatch)
    fake.publish_rc = mqtt.MQTT_ERR_NO_CONN

    start = time.monotonic()
    ok = uplink._publish(uplink._topic_meas, {"items": []})
    elapsed = time.monotonic() - start

    assert ok is False
    assert elapsed < 1.0, "rc != SUCCESS phai tra ve NGAY, khong duoc cho _ACK_TIMEOUT_S (5s mac dinh)"


def test_publish_with_wait_ack_false_returns_true_without_waiting_for_puback(monkeypatch):
    """wait_ack=False (dung boi _on_connect - xem docstring module: goi
    wait_for_publish() ngay tren network thread cua paho se tu khoa chinh no)
    phai tra ve True NGAY sau khi publish() thanh cong, KHONG cho PUBACK."""
    uplink, fake = _make_uplink(monkeypatch)
    fake.auto_ack = False          # PUBACK khong bao gio toi

    start = time.monotonic()
    ok = uplink._publish(uplink._topic_status, {"online": True}, wait_ack=False)
    elapsed = time.monotonic() - start

    assert ok is True
    assert elapsed < 1.0, "wait_ack=False phai tra ve NGAY, khong duoc cho PUBACK"


def test_measurements_publishes_to_meas_topic_with_items_bid_seq(monkeypatch):
    uplink, fake = _make_uplink(monkeypatch)

    ok = uplink.measurements([{"ch": "a", "v": 1}], "bid-x", 7)

    assert ok is True
    topic, payload, qos, retain = fake.published[0]
    assert topic == uplink._topic_meas
    body = json.loads(payload)
    assert body["items"] == [{"ch": "a", "v": 1}]
    assert body["bid"] == "bid-x" and body["seq"] == 7


def test_ack_command_publishes_to_cmdack_topic(monkeypatch):
    uplink, fake = _make_uplink(monkeypatch)

    ok = uplink.ack_command(42, True, "")

    assert ok is True
    topic, payload, qos, retain = fake.published[0]
    assert topic == uplink._topic_cmd_ack
    body = json.loads(payload)
    assert body["id"] == 42 and body["ok"] is True and body["detail"] == ""


# ----------------------------------------------------------------------
# 3) _on_connect() - subscribe cmd + publish status {"online": true, "cmd": true}
#    KHONG cho PUBACK (wait_ack=False - tranh tu khoa network thread cua paho)

def test_on_connect_subscribes_cmd_and_publishes_status_with_cmd_true(monkeypatch):
    uplink, fake = _make_uplink(monkeypatch)
    fake.auto_ack = False   # neu _on_connect lo cho PUBACK, test nay se treo/fail

    uplink._on_connect(fake, None, None, 0)

    assert fake.subscribed == [(uplink._topic_cmd, 1)]
    topic, payload, qos, retain = fake.published[0]
    assert topic == uplink._topic_status
    assert retain is True
    body = json.loads(payload)
    assert body["online"] is True
    assert body["cmd"] is True
    assert "sig" in body, "status connect-time phai duoc KY (khac LWT - xem docstring module)"


def test_on_connect_does_nothing_when_rc_nonzero(monkeypatch):
    uplink, fake = _make_uplink(monkeypatch)

    uplink._on_connect(fake, None, None, 5)

    assert fake.subscribed == []
    assert fake.published == []


def test_lwt_registered_at_init_is_not_signed(monkeypatch):
    """LWT (broker tu phat khi mat TCP) duoc dang ky 1 lan trong __init__,
    KHONG di qua _publish()/sign() - luc broker phat no client da mat ket
    noi nen khong the ky dong duoc (xem docstring module)."""
    uplink, fake = _make_uplink(monkeypatch)

    topic, payload, qos, retain = fake.will

    assert topic == uplink._topic_status
    assert retain is True
    body = json.loads(payload)
    assert body == {"online": False}


# ----------------------------------------------------------------------
# 4) _on_message() / next_command() - KHONG block, KHONG raise voi JSON hong

def test_on_message_valid_json_pushed_to_queue(monkeypatch):
    uplink, _ = _make_uplink(monkeypatch)
    msg = SimpleNamespace(payload=json.dumps({"id": 1, "channel": "a", "cmd": "write"}).encode())

    uplink._on_message(None, None, msg)

    assert uplink.next_command(0.2) == {"id": 1, "channel": "a", "cmd": "write"}


def test_on_message_invalid_json_is_dropped_not_raised(monkeypatch):
    uplink, _ = _make_uplink(monkeypatch)
    msg = SimpleNamespace(payload=b"not-json{")

    uplink._on_message(None, None, msg)          # KHONG duoc raise

    assert uplink.next_command(0.05) is None


def test_next_command_returns_none_on_empty_queue_timeout(monkeypatch):
    uplink, _ = _make_uplink(monkeypatch)

    assert uplink.next_command(0.05) is None
