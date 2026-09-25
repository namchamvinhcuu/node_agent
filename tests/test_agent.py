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
