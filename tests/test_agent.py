# -*- coding: utf-8 -*-
"""Test node_agent/agent.py (NodeAgent) - tap trung vao Huong B (ModbusReader
"points": 1 reader can dai dien NHIEU channel code) va invariant dedup command
theo id (khop parity ESP32, xem CLAUDE.md muc 6/6b): NodeAgent._build_readers()
phai map TAT CA code cua 1 nguon multi-point ve CUNG 1 object reader, start()/
stop() khong duoc goi 2 lan tren cung 1 reader du no xuat hien nhieu lan trong
self._readers.values(), va _command_loop() khong duoc de lenh thanh cong cho
1 code lam lenh KHAC id cho code khac bi coi la "da thuc thi roi".

NodeAgent() that su tao Store(sqlite that, dung threading.Lock) + EdgeClient
(requests.Session(), KHONG goi mang trong __init__) - an toan de khoi tao that
trong test mien la monkeypatch settings.state_dir/channels_file sang tmp_path
(tranh dung ./var that cua project)."""
import json
import threading
import time

from unittest.mock import Mock

import node_agent.agent as agent_module
import node_agent.config as config_module
from node_agent.agent import NodeAgent


class _FakeThread:
    """Thay the threading.Thread trong test start()/stop() - KHONG duoc chay
    that cac loop nen (hello/flush/sender/heartbeat/command/setup_server:
    goi mang that, mo uvicorn that). start()/join() deu no-op, chi ghi nhan
    da duoc goi de start()/stop() cua NodeAgent chay het logic cua no (bao
    gom vong for qua set(self._readers.values()))."""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target = target
        self._args = args

    def start(self):
        pass

    def join(self, timeout=None):
        pass


def _make_agent(monkeypatch, tmp_path, channels=None):
    monkeypatch.setattr(config_module.settings, "state_dir", tmp_path)
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps(channels or []), encoding="utf-8")
    monkeypatch.setattr(config_module.settings, "channels_file", str(channels_path))
    return NodeAgent()


def _multipoint_modbus_cfg():
    return {
        "code": "temp_humid_src", "mode": "modbus", "conn_type": "tcp",
        "host": "10.0.0.9", "tcp_port": 502, "unit_id": 1,
        "register_type": "holding", "address": 0, "poll_ms": 1000,
        "points": [
            {"code": "a", "reg_offset": 0, "data_type": "u16"},
            {"code": "b", "reg_offset": 1, "data_type": "u16"},
        ],
    }


# ----------------------------------------------------------------------
# 1) _build_readers() - Huong B: 1 nguon multi-point map ve CUNG 1 reader

def test_build_readers_multipoint_source_maps_all_codes_to_same_reader_instance(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path, channels=[_multipoint_modbus_cfg()])

    agent._build_readers()

    assert "a" in agent._readers and "b" in agent._readers
    assert agent._readers["a"] is agent._readers["b"]


def test_build_readers_single_point_channel_unaffected(monkeypatch, tmp_path):
    """Regression tuong thich nguoc: kenh KHONG dung "points" van map 1-1
    code -> reader rieng, khong bi anh huong boi thay doi Huong B."""
    agent = _make_agent(monkeypatch, tmp_path, channels=[
        {"code": "solo", "mode": "sim", "poll_ms": 500, "center": 10, "spread": 0.1},
    ])

    agent._build_readers()

    assert list(agent._readers.keys()) == ["solo"]


# ----------------------------------------------------------------------
# 2) start()/stop() - dedupe by object identity, KHONG goi start()/stop()
#    2 lan tren CUNG 1 reader multi-point

def test_start_stop_calls_reader_start_and_stop_exactly_once_for_shared_multipoint_reader(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader_mock = Mock()
    monkeypatch.setattr(agent, "_build_readers", lambda: None)
    agent._readers = {"a": reader_mock, "b": reader_mock}  # CUNG 1 object, xuat hien 2 lan
    monkeypatch.setattr(agent_module.threading, "Thread", _FakeThread)

    agent.start()

    assert reader_mock.start.call_count == 1, (
        "reader.start() bi goi %d lan - phai dung set() de dedupe theo identity, "
        "khong duoc spawn 2 thread cho CUNG 1 reader object" % reader_mock.start.call_count)

    agent.stop()

    assert reader_mock.stop.call_count == 1, (
        "reader.stop() bi goi %d lan thay vi dung 1" % reader_mock.stop.call_count)


# ----------------------------------------------------------------------
# 3) _command_loop() - dedup theo cmd_id, KHONG lan lon giua 2 code cung 1
#    reader multi-point (khop parity ESP32 - xem CLAUDE.md muc 6/6b)

def test_command_loop_dedup_by_exact_id_not_by_channel_or_recent_success(monkeypatch, tmp_path):
    """3 lenh DIFFERENT id cho 2 code khac nhau (cung 1 reader multi-point)
    deu phai duoc THUC THI (khong bi coi la trung lap chi vi mot lenh KHAC
    vua thanh cong truoc do) - roi 1 lenh REDELIVERY dung id vua thanh cong
    (id=102, code "a") phai duoc DEDUP (chi ack lai, khong thuc thi lai)."""
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.return_value = {"ok": True}
    agent._readers = {"a": reader, "b": reader}

    calls = [
        {"command": {"id": 100, "channel": "a", "cmd": "write", "value": 1}},
        {"command": {"id": 101, "channel": "b", "cmd": "write", "value": 2}},
        {"command": {"id": 102, "channel": "a", "cmd": "write", "value": 3}},
        {"command": {"id": 102, "channel": "a", "cmd": "write", "value": 3}},  # redelivery
        {},
    ]
    agent.client = Mock()
    agent.client.next_command.side_effect = calls
    ack_calls = []
    agent.client.ack_command.side_effect = lambda cmd_id, ok, detail="": ack_calls.append((cmd_id, ok, detail))

    def fake_wait(timeout=None):
        agent._stop.set()   # chi dat khi khong co lenh (nhanh cuoi cua vong lap)
        return False

    monkeypatch.setattr(agent._stop, "wait", fake_wait)

    agent._command_loop()

    # 3 lenh DIFFERENT id deu duoc thuc thi that (KHONG bi dedup lan nhau du
    # xen ke 2 code tren cung 1 reader) - day la diem chinh cua invariant.
    assert reader.command.call_count == 3, (
        "reader.command() duoc goi %d lan thay vi 3 - mot lenh id KHAC bi "
        "dedup nham chi vi CO mot lenh khac vua thanh cong truoc do (dedup "
        "phai so sanh CHINH XAC cmd_id, khong phai 'gan day co thanh cong "
        "hay khong')" % reader.command.call_count)
    reader.command.assert_any_call("write", 1, channel="a")
    reader.command.assert_any_call("write", 2, channel="b")
    reader.command.assert_any_call("write", 3, channel="a")

    # Redelivery dung id=102 (lan 4) phai duoc ACK LAI nhung KHONG thuc thi
    # lai - tong so lan ack la 4 (3 thuc thi that + 1 redelivery), nhung
    # command() van chi 3 lan nhu tren.
    assert ack_calls == [(100, True, ""), (101, True, ""), (102, True, ""), (102, True, "")]


def test_command_loop_routes_command_with_correct_channel_param(monkeypatch, tmp_path):
    """reader.command(...) phai duoc goi KEM channel=cmd["channel"] dung -
    agent.py phai truyen tham so nay xuong de ModbusReader (Huong B) nham
    dung point trong nguon multi-point."""
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.return_value = {"ok": True}
    agent._readers = {"b": reader}

    agent.client = Mock()
    agent.client.next_command.side_effect = [
        {"command": {"id": 1, "channel": "b", "cmd": "write", "value": 9.5}},
        {},
    ]

    def fake_wait(timeout=None):
        agent._stop.set()
        return False

    monkeypatch.setattr(agent._stop, "wait", fake_wait)

    agent._command_loop()

    reader.command.assert_called_once_with("write", 9.5, channel="b")


# ----------------------------------------------------------------------
# 4) Nhanh MQTT (self.mqtt truthy) - durable-first (_sender_loop) va dedup
#    theo id (_command_loop) phai giu NGUYEN invariant nhu nhanh HTTP
#    (CLAUDE.md muc 6/6b, "2 loai node khong duoc lech hanh vi voi cung 1 edge")

def _stop_after_first_wait(monkeypatch, agent):
    """Patch THANG threading.Event.wait() (class method, khong phai 1
    instance cu the) de dung loop sau DUNG 1 vong lap, bat ke agent dang cho
    qua _stop.wait() hay qua 1 'kick' Event khac (vd _sender_kick/_flush_kick,
    them 2026-09-29 - khong con doan nao trong _sender_loop/_flush_loop chac
    chan goi _stop.wait() nua, patch rieng agent._stop.wait se KHONG dung duoc
    loop va treo test that su cho toi khi bi kill). An toan cho test nay vi
    chi goi 1 lan roi set agent._stop ngay, khong quan tam wait() nao goi no."""
    def fake_wait(self, timeout=None):
        agent._stop.set()
        return False
    monkeypatch.setattr(threading.Event, "wait", fake_wait)


def test_sender_loop_mqtt_publish_failure_keeps_outbox_row(monkeypatch, tmp_path):
    """Durable-first phai dung o CA nhanh MQTT: publish khong nhan PUBACK
    (mqtt.measurements() tra False) KHONG duoc xoa outbox row - du lieu
    khong duoc mat chi vi mat ket noi MQTT tam thoi."""
    agent = _make_agent(monkeypatch, tmp_path)
    agent.mqtt = Mock()
    agent.mqtt.measurements.return_value = False
    agent.store.outbox_push("bid1", 1, {"items": [{"ch": "a", "v": 1}]})
    _stop_after_first_wait(monkeypatch, agent)

    agent._sender_loop()

    assert agent.store.outbox_count() == 1, (
        "outbox row bi xoa du publish MQTT that bai - vi pham durable-first")
    agent.mqtt.measurements.assert_called_once_with([{"ch": "a", "v": 1}], "bid1", 1)


def test_sender_loop_mqtt_publish_success_deletes_outbox_row(monkeypatch, tmp_path):
    """Doi chung: publish MQTT THANH CONG (co PUBACK) phai xoa outbox row
    giong het nhanh HTTP - khong regress hanh vi khi bat MQTT uplink."""
    agent = _make_agent(monkeypatch, tmp_path)
    agent.mqtt = Mock()
    agent.mqtt.measurements.return_value = True
    agent.store.outbox_push("bid1", 1, {"items": [{"ch": "a", "v": 1}]})
    _stop_after_first_wait(monkeypatch, agent)

    agent._sender_loop()

    assert agent.store.outbox_count() == 0


def test_command_loop_mqtt_dedup_by_exact_id_only_acks_again(monkeypatch, tmp_path):
    """Cung invariant dedup o test_command_loop_dedup_by_exact_id_... o tren,
    nhung qua nhanh MQTT (self.mqtt.next_command thay self.client.next_command)
    - lenh REDELIVERY dung id vua thanh cong chi duoc ACK LAI qua
    mqtt.ack_command(), KHONG goi lai reader.command()."""
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.return_value = {"ok": True}
    agent._readers = {"a": reader}
    agent.mqtt = Mock()

    remaining = iter([
        {"id": 300, "channel": "a", "cmd": "write", "value": 1},
        {"id": 300, "channel": "a", "cmd": "write", "value": 1},   # redelivery
    ])

    def fake_next_command(timeout):
        cmd = next(remaining, None)
        if cmd is None:
            agent._stop.set()
        return cmd
    agent.mqtt.next_command.side_effect = fake_next_command

    agent._command_loop()

    assert reader.command.call_count == 1, (
        "reader.command() bi goi lai cho lenh redelivery id=300 - dedup nhanh "
        "MQTT phai giong het nhanh HTTP (invariant CLAUDE.md muc 6/6b)")
    reader.command.assert_called_once_with("write", 1, channel="a")
    assert agent.mqtt.ack_command.call_count == 2
    agent.mqtt.ack_command.assert_any_call(300, True, "")


def test_command_loop_mqtt_branch_never_calls_http_client(monkeypatch, tmp_path):
    """Khi self.mqtt duoc bat, nhanh HTTP (self.client.next_command/
    ack_command) KHONG duoc goi - tranh double-ack/double-poll qua ca 2
    duong cung luc."""
    agent = _make_agent(monkeypatch, tmp_path)
    agent._readers = {}
    agent.mqtt = Mock()
    agent.client = Mock()

    def fake_next_command(timeout):
        agent._stop.set()
        return None
    agent.mqtt.next_command.side_effect = fake_next_command

    agent._command_loop()

    agent.client.next_command.assert_not_called()
    agent.client.ack_command.assert_not_called()


# ----------------------------------------------------------------------
# 5) "Kick" pattern (_flush_kick/_sender_kick, 2026-09-29) - _emit() va
#    outbox_push() phai danh thuc loop GAN NHU NGAY thay vi doi het fallback
#    timer co dinh (khop pattern ESP32 xTaskNotifyGive/ulTaskNotifyTake).
#    Test nay chay _flush_loop()/_sender_loop() tren THREAD THAT (khong goi
#    dong bo trong test thread nhu cac test tren) vi muon do THOI GIAN thuc -
#    khong dung _stop_after_first_wait (patch Event.wait toan cuc se lam kick
#    tra ve NGAY LAP TUC, khong con do duoc "nhanh hon fallback" nua).

def _run_loop_in_thread(fn):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


def _wait_until(predicate, timeout=1.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_threading_event_set_before_wait_is_not_lost():
    """Ghi lai invariant threading.Event ma kick pattern dang dua vao: set()
    xay ra TRUOC wait() khong lam mat tin hieu (wait() kiem tra co truoc khi
    block) - day la ly do _flush_kick/_sender_kick.clear() luon goi SAU khi
    wait() tra ve (khong phai truoc), tranh race giua producer va consumer."""
    ev = threading.Event()
    ev.set()                     # "kick" den TRUOC khi consumer kip goi wait()

    start = time.monotonic()
    woke = ev.wait(5.0)          # phai tra ve NGAY, khong doi het 5s
    elapsed = time.monotonic() - start

    assert woke is True
    assert elapsed < 0.5


def test_flush_loop_emit_wakes_up_immediately_not_waiting_full_submit_interval(monkeypatch, tmp_path):
    """_emit() set _flush_kick - _flush_loop phai thuc day GAN NHU NGAY, khong
    doi het submit_interval_s. Patch interval RAT LON (10s) de chung minh kick
    thang timer, khong phai trung hop do interval von da ngan."""
    agent = _make_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(config_module.settings, "submit_interval_s", 10.0)
    t = _run_loop_in_thread(agent._flush_loop)

    try:
        start = time.monotonic()
        agent._emit("a", 1.0, "OK", 0, True)
        got = _wait_until(lambda: agent.store.outbox_count() > 0)
        elapsed = time.monotonic() - start

        assert got, "outbox khong co du lieu - flush khong duoc kick danh thuc"
        assert elapsed < 1.0, (
            "flush cho lau hon 1s (submit_interval_s dang la 10s) - kick khong "
            "thang duoc fallback timer")
    finally:
        agent._stop.set()
        agent._flush_kick.set()   # unblock wait() dang treo (neu con) de thread thoat sach
        t.join(timeout=2)


def test_flush_loop_falls_back_to_timer_when_no_kick(monkeypatch, tmp_path):
    """Khong co _emit() nao goi (khong ai kick) - _flush_loop VAN phai thuc
    day dung theo fallback timer (submit_interval_s) va flush item da co san
    trong _pending, khong bi 'ket' cho _flush_kick vinh vien (hanh vi batch
    khi idle - khong pha bang cach thay _stop.wait() bang _flush_kick.wait()
    ma quen giu duong fallback nay)."""
    agent = _make_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(config_module.settings, "submit_interval_s", 0.05)
    # Dua thang vao _pending, KHONG qua _emit() - mo phong item da san sang
    # nhung KHONG co kick nao duoc bat (giong luc idle giua 2 lan _emit()).
    agent._pending.put_nowait({"ch": "a", "v": 1, "s": "OK", "q": 0, "stable": True, "ts": 0})
    t = _run_loop_in_thread(agent._flush_loop)

    try:
        got = _wait_until(lambda: agent.store.outbox_count() > 0, timeout=1.0)
        assert got, "fallback timer khong flush item da co san khi khong co kick"
    finally:
        agent._stop.set()
        agent._flush_kick.set()
        t.join(timeout=2)


def test_sender_loop_wakes_up_immediately_after_outbox_push_kick(monkeypatch, tmp_path):
    """Sau outbox_push() (do _flush_loop lam) kem _sender_kick.set(), sender
    phai thuc day nhanh hon 1.0s fallback cu."""
    agent = _make_agent(monkeypatch, tmp_path)
    agent.mqtt = Mock()
    agent.mqtt.measurements.return_value = True
    t = _run_loop_in_thread(agent._sender_loop)

    try:
        start = time.monotonic()
        agent.store.outbox_push("bid1", 1, {"items": []})
        agent._sender_kick.set()      # dung dung co che that: _flush_loop set kick SAU outbox_push
        got = _wait_until(lambda: agent.mqtt.measurements.call_count > 0)
        elapsed = time.monotonic() - start

        assert got
        assert elapsed < 1.0, "sender cho lau hon 1.0s fallback - kick khong danh thuc dung"
        agent.mqtt.measurements.assert_called_once_with([], "bid1", 1)
    finally:
        agent._stop.set()
        agent._sender_kick.set()
        t.join(timeout=2)


def test_sender_loop_http_legacy_still_works_with_kick(monkeypatch, tmp_path):
    """Regression: nhanh HTTP legacy (self.mqtt is None) van dung chung
    _sender_loop (nay co them nhanh kick) - hanh vi publish/durable-first
    KHONG doi, chi nhanh hon (kick ap dung CA 2 nhanh, khong rieng MQTT)."""
    agent = _make_agent(monkeypatch, tmp_path)
    agent.mqtt = None
    agent.client = Mock()
    agent.client.measurements.return_value = {"ok": True}
    t = _run_loop_in_thread(agent._sender_loop)

    try:
        agent.store.outbox_push("bid1", 1, {"items": [{"ch": "a", "v": 1}]})
        agent._sender_kick.set()
        got = _wait_until(lambda: agent.store.outbox_count() == 0)

        assert got, "outbox row khong duoc xoa - nhanh HTTP legacy bi anh huong boi kick refactor"
        agent.client.measurements.assert_called_once_with([{"ch": "a", "v": 1}], "bid1", 1)
    finally:
        agent._stop.set()
        agent._sender_kick.set()
        t.join(timeout=2)
