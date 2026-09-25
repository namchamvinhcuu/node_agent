# -*- coding: utf-8 -*-
"""Test node_agent/readers/mqtt.py (MqttReader) - reader MQTT moi, CHUA co
broker MQTT that trong moi truong dev (Nam xac nhan giong Modbus) nen toan bo
test o day dung mock cho paho-mqtt (khong co broker that).

Luu y ve mock: MqttReader import paho.mqtt.client O MODULE-LEVEL trong
node_agent/readers/mqtt.py (`import paho.mqtt.client as mqtt`), nen phai
patch dung namespace SU DUNG `node_agent.readers.mqtt.mqtt.Client` (KHONG
phai `paho.mqtt.client.Client` - patch tai nguon dinh nghia se KHONG co tac
dung len ten da bind vao module mqtt.py tu luc import). Dung pattern giong
het test_modbus_reader.py (patch `node_agent.readers.modbus.ModbusTcpClient`)."""
import threading
import time
from unittest.mock import MagicMock, Mock, patch

import paho.mqtt.client as mqtt

from node_agent.readers.mqtt import MqttReader, _parse_payload


# ----------------------------------------------------------------------
# 1) _parse_payload - ham thuan, khong can mock

def test_parse_payload_raw_number_no_json_key():
    value, err = _parse_payload(b"23.5", "")
    assert value == 23.5
    assert err is None


def test_parse_payload_raw_number_not_numeric_no_json_key():
    value, err = _parse_payload(b"abc", "")
    assert value is None
    assert "khong phai so" in err


def test_parse_payload_json_key_valid_match():
    value, err = _parse_payload(b'{"value": 12.3}', "value")
    assert value == 12.3
    assert err is None


def test_parse_payload_json_key_invalid_json_syntax():
    value, err = _parse_payload(b"{not json", "value")
    assert value is None
    assert "JSON khong hop le" in err


def test_parse_payload_json_key_missing_key():
    value, err = _parse_payload(b'{"other": 1}', "value")
    assert value is None
    assert "thieu key" in err
    assert "value" in err


def test_parse_payload_json_key_value_not_a_number():
    value, err = _parse_payload(b'{"value": "abc"}', "value")
    assert value is None
    assert "khong phai so" in err


def test_parse_payload_json_key_payload_not_a_dict():
    """JSON hop le nhung khong phai object (vd so tho) - phai bao loi "thieu
    key" (isinstance check), khong duoc crash vi TypeError khi tra
    `json_key not in data` tren mot int/float (khong iterable). Dung so tho
    (khong phai list) vi "x" not in [1,2,3] van chay binh thuong (list
    iterable) - khong lam lo mutation bo isinstance check; int thi lo ngay."""
    value, err = _parse_payload(b"42", "value")
    assert value is None
    assert "thieu key" in err


def test_parse_payload_value_rounding_precision_not_lost_in_parse():
    """_parse_payload chi tra float tho, viec round(...,6) la o _on_message -
    dam bao _parse_payload KHONG tu lam tron som lam mat precision."""
    value, err = _parse_payload(b"23.123456789", "")
    assert err is None
    assert value == 23.123456789


# ----------------------------------------------------------------------
# 2) MqttReader._on_connect

def _cfg(**overrides):
    cfg = {"code": "ch1", "topic": "sensor/ch1"}
    cfg.update(overrides)
    return cfg


def test_on_connect_rc_zero_sets_online_and_subscribes_topic():
    reader = MqttReader(_cfg(topic="sensor/ch1"), emit=Mock())
    client = MagicMock()

    reader._on_connect(client, None, {}, 0)

    assert reader.status.online is True
    assert reader.status.error is None
    client.subscribe.assert_called_once_with("sensor/ch1")


def test_on_connect_rc_nonzero_sets_offline_and_error_contains_rc():
    reader = MqttReader(_cfg(), emit=Mock())
    client = MagicMock()

    reader._on_connect(client, None, {}, 5)

    assert reader.status.online is False
    assert "5" in reader.status.error
    client.subscribe.assert_not_called()


# ----------------------------------------------------------------------
# 3) MqttReader._on_disconnect

def test_on_disconnect_nonzero_rc_sets_offline_and_error():
    reader = MqttReader(_cfg(), emit=Mock())

    reader._on_disconnect(None, None, 7)

    assert reader.status.online is False
    assert "7" in reader.status.error


def test_on_disconnect_rc_zero_still_marks_offline_but_no_error():
    """_on_disconnect luon dat online=False bat ke rc (mat ket noi la mat
    ket noi, kha nang reconnect thanh cong sau do khong lam thay doi su
    kien nay) - chi rc!=0 moi ghi status.error."""
    reader = MqttReader(_cfg(), emit=Mock())

    reader._on_disconnect(None, None, 0)

    assert reader.status.online is False
    assert reader.status.error is None


def test_on_connect_fail_sets_offline_and_error():
    """Regression cho finding Major #2: khi host/port sai (vd typo trong
    channels.json), paho tu retry vo han trong network loop rieng va
    _on_connect/_on_disconnect chua tung fire (chua ket noi thanh cong lan
    nao de roi bi ngat) - _on_connect_fail la callback DUY NHAT bao dong
    tinh trang nay, phai set status.online=False + status.error."""
    reader = MqttReader(_cfg(), emit=Mock())
    client = MagicMock()

    reader._on_connect_fail(client, None)

    assert reader.status.online is False
    assert reader.status.error is not None


# ----------------------------------------------------------------------
# 4) MqttReader._on_message

def test_on_message_raw_number_emits_rounded_value_quality_0_stable_true():
    emitted = []
    reader = MqttReader(_cfg(), emit=lambda *a: emitted.append(a))
    msg = Mock()
    msg.payload = b"23.123456789"

    reader._on_message(None, None, msg)

    assert emitted == [("ch1", round(23.123456789, 6), None, 0, True)]


def test_on_message_json_key_valid_emits_rounded_value():
    emitted = []
    reader = MqttReader(_cfg(json_key="value"), emit=lambda *a: emitted.append(a))
    msg = Mock()
    msg.payload = b'{"value": 12.3456789}'

    reader._on_message(None, None, msg)

    assert emitted == [("ch1", round(12.3456789, 6), None, 0, True)]


def test_on_message_parse_error_emits_quality_2_and_sets_status_error():
    emitted = []
    reader = MqttReader(_cfg(), emit=lambda *a: emitted.append(a))
    msg = Mock()
    msg.payload = b"khong phai so"

    reader._on_message(None, None, msg)

    assert emitted == [("ch1", None, None, 2, False)]
    assert reader.status.error is not None


def test_on_message_json_missing_key_emits_error():
    emitted = []
    reader = MqttReader(_cfg(json_key="temp"), emit=lambda *a: emitted.append(a))
    msg = Mock()
    msg.payload = b'{"humidity": 50}'

    reader._on_message(None, None, msg)

    assert emitted == [("ch1", None, None, 2, False)]
    assert "temp" in reader.status.error


def test_on_message_json_invalid_syntax_emits_error():
    emitted = []
    reader = MqttReader(_cfg(json_key="value"), emit=lambda *a: emitted.append(a))
    msg = Mock()
    msg.payload = b"{broken"

    reader._on_message(None, None, msg)

    assert emitted == [("ch1", None, None, 2, False)]


# ----------------------------------------------------------------------
# 5) MqttReader._run() - vong doi thuc: connect_async -> loop_start -> wait
# -> loop_stop -> disconnect. Chay trong thread rieng + join(timeout=...)
# thay vi goi _run() truc tiep tren thread test - dung pattern hardening da
# ap dung o test_modbus_reader.py::test_run_backoff_applies_to_read_failures
# _not_just_connect_failures: neu mot regression tuong lai lam self._stop.wait()
# khong bao gio tra ve (vd doi thanh vong lap vo han khong kiem tra _stop),
# goi truc tiep se treo CA SUITE vinh vien; thread + join(timeout) + assert
# not t.is_alive() bien treo do thanh 1 FAIL ro rang.

def _run_in_thread_until_stop(reader, settle_s=0.05):
    t = threading.Thread(target=reader._run, daemon=True)
    t.start()
    time.sleep(settle_s)  # cho _run() kip chay toi self._stop.wait()
    reader._stop.set()
    t.join(timeout=2)
    return t


def test_run_happy_path_connects_loops_and_cleans_up_on_stop():
    reader = MqttReader(_cfg(host="10.0.0.9", port=1884), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client) as ctor:
        t = _run_in_thread_until_stop(reader)

    assert not t.is_alive()
    ctor.assert_called_once_with()
    fake_client.connect_async.assert_called_once_with("10.0.0.9", 1884, keepalive=30)
    fake_client.username_pw_set.assert_not_called()
    fake_client.loop_start.assert_called_once()
    fake_client.loop_stop.assert_called_once()
    fake_client.disconnect.assert_called_once()
    assert reader._client is None


def test_run_sets_callbacks_to_reader_bound_methods():
    reader = MqttReader(_cfg(), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client):
        t = _run_in_thread_until_stop(reader)

    assert not t.is_alive()
    assert fake_client.on_connect == reader._on_connect
    assert fake_client.on_disconnect == reader._on_disconnect
    assert fake_client.on_connect_fail == reader._on_connect_fail
    assert fake_client.on_message == reader._on_message


def test_run_enables_paho_internal_logger():
    """Regression cho finding Major #2: `client.enable_logger(_logger)` phai
    duoc goi de log DEBUG noi bo cua paho (vd chi tiet retry connect) chay
    chung kenh voi logger cua node_agent, khong bi im lang rieng."""
    reader = MqttReader(_cfg(), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client):
        t = _run_in_thread_until_stop(reader)

    assert not t.is_alive()
    fake_client.enable_logger.assert_called_once()


def test_run_with_username_calls_username_pw_set():
    reader = MqttReader(_cfg(username="admin", password="secret"), emit=Mock())  # secret-allow: fixture gia lap, khong phai credential that
    fake_client = MagicMock()

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client):
        t = _run_in_thread_until_stop(reader)

    assert not t.is_alive()
    fake_client.username_pw_set.assert_called_once_with("admin", "secret")


def test_run_without_username_configured_does_not_set_password_empty_string_bug():
    """password rong khi KHONG khai bao username - danh dau ranh gioi voi
    truong hop co username nhung khong co password (dung "" thay None)."""
    reader = MqttReader(_cfg(username="admin"), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client):
        t = _run_in_thread_until_stop(reader)

    assert not t.is_alive()
    fake_client.username_pw_set.assert_called_once_with("admin", "")


def test_run_uses_default_host_and_port_when_not_configured():
    reader = MqttReader(_cfg(), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client):
        t = _run_in_thread_until_stop(reader)

    assert not t.is_alive()
    fake_client.connect_async.assert_called_once_with("127.0.0.1", 1883, keepalive=30)


def test_run_connect_async_exception_sets_error_and_returns_without_looping():
    """connect_async() co the raise NGAY (vd loi resolve host/DNS) truoc khi
    kip giao viec cho network loop rieng - phai return SOM, KHONG duoc goi
    loop_start()/self._stop.wait(), va self._client phai ve None. Goi _run()
    TRUC TIEP (khong can thread) vi ham return truoc khi cham self._stop.wait()."""
    reader = MqttReader(_cfg(host="ten-mien-khong-ton-tai.invalid"), emit=Mock())
    fake_client = MagicMock()
    fake_client.connect_async.side_effect = OSError("Name or service not known")

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client):
        reader._run()

    assert reader.status.online is False
    assert "Name or service not known" in reader.status.error
    assert reader._client is None
    fake_client.loop_start.assert_not_called()


def test_run_port_not_castable_to_int_sets_error_and_returns_without_crashing():
    """Regression cho finding Major #1: channels.json co the bi sua tay
    ngoai UI setup (comment agent.py dong 65-71) - `port` sai kieu (vd chuoi
    rong "" thay vi so) truoc day nam NGOAI try/except (chi boc quanh
    connect_async) nen se lam thread nay CHET IM LANG VINH VIEN, khong
    log/khong status.error. Gio `int(self.cfg.get("port", 1883))` nam
    TRONG cung try/except voi connect_async - ValueError phai duoc bat va
    xu ly y het loi connect_async, KHONG duoc de thread crash/treo.

    Chay qua _run_in_thread_until_stop() (thread + join(timeout)) du ham
    nay du kien return SOM (khong toi self._stop.wait()) - phong truong
    hop regression tuong lai lam no rot vao nhanh block vo han."""
    reader = MqttReader(_cfg(port=""), emit=Mock())
    fake_client = MagicMock()

    with patch("node_agent.readers.mqtt.mqtt.Client", return_value=fake_client):
        t = _run_in_thread_until_stop(reader, settle_s=0.05)

    assert not t.is_alive()
    assert reader.status.online is False
    assert reader.status.error is not None
    assert reader._client is None
    fake_client.connect_async.assert_not_called()
    fake_client.loop_start.assert_not_called()


# ----------------------------------------------------------------------
# 6) MqttReader.command()
#
# Sau finding Critical cua python-reviewer 2026-09-25 (verify tren source
# paho-mqtt that: publish() KHONG raise khi mat ket noi, chi tra info.rc=
# MQTT_ERR_NO_CONN va DROP message vinh vien), command() gio kiem CA
# self._client CA self.status.online TRUOC khi publish, roi kiem lai
# info.rc SAU khi publish - "co client" khong con du de coi la "da ket noi",
# phai set status.online=True moi qua duoc nhanh happy-path.

def _connected_reader(**cfg_overrides):
    reader = MqttReader(_cfg(**cfg_overrides), emit=Mock())
    reader._client = MagicMock()
    reader._client.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    reader.status.online = True
    return reader


def test_command_returns_error_dict_when_not_connected_does_not_crash():
    reader = MqttReader(_cfg(), emit=Mock())

    result = reader.command("write", 5)

    assert result == {"ok": False, "error": "chua ket noi toi broker"}


def test_command_returns_error_dict_when_client_present_but_status_offline():
    """Regression cho finding Critical: co self._client (da tung connect())
    nhung status.online=False (vd da bi disconnect, dang cho paho tu
    reconnect ngam) - KHONG duoc goi publish(), phai tra ok=False ngay,
    tranh agent.py::_command_loop nham "da publish thanh cong" roi dedup
    mat lenh that su chua bao gio toi broker."""
    reader = MqttReader(_cfg(topic="sensor/ch1"), emit=Mock())
    reader._client = MagicMock()
    reader.status.online = False

    result = reader.command("write", 5)

    assert result == {"ok": False, "error": "chua ket noi toi broker"}
    reader._client.publish.assert_not_called()


def test_command_returns_error_when_publish_rc_reports_no_conn():
    """Regression cho finding Critical: publish() cua paho KHONG raise khi
    mat ket noi - chi tra info.rc=MQTT_ERR_NO_CONN va DROP message vinh
    vien (verify tren source paho-mqtt 1.6.1 that: _send_publish()). command()
    phai doc info.rc SAU khi goi publish() va tra ok=False neu rc bao loi,
    khong duoc coi "publish() khong raise" = "da gui thanh cong"."""
    reader = _connected_reader(topic="sensor/ch1")
    reader._client.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_NO_CONN)

    result = reader.command("write", 42)

    assert result["ok"] is False
    assert str(mqtt.MQTT_ERR_NO_CONN) in result["error"]


def test_command_publishes_to_default_cmd_topic_derived_from_topic():
    reader = _connected_reader(topic="sensor/ch1")

    result = reader.command("write", 42)

    reader._client.publish.assert_called_once_with("sensor/ch1/cmd", "42")
    assert result == {"ok": True, "status": "ok"}


def test_command_publishes_to_explicit_cmd_topic_when_configured():
    reader = _connected_reader(topic="sensor/ch1", cmd_topic="control/ch1")

    reader.command("write", 7)

    reader._client.publish.assert_called_once_with("control/ch1", "7")


def test_command_value_none_uses_cmd_name_as_payload():
    reader = _connected_reader(topic="sensor/ch1")

    reader.command("zero", None)

    reader._client.publish.assert_called_once_with("sensor/ch1/cmd", "zero")


def test_command_publish_exception_returns_error_dict_not_crash():
    reader = _connected_reader()
    reader._client.publish.side_effect = RuntimeError("broker tu choi")

    result = reader.command("write", 1)

    assert result["ok"] is False
    assert "broker tu choi" in result["error"]
