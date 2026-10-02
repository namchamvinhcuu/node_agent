# -*- coding: utf-8 -*-
"""Test NodeAgent._command_loop() cho task pcm-downlink-command (2026-10-02):
(1) lenh hong KHONG lam crash loop (crash -> _guarded() restart -> mat cua so
dedup), (2) dedup theo cua so _CMD_DEDUP_WINDOW khoa (request_id uu tien, khong
co thi id), chi nho lenh THANH CONG, (3) verify HMAC + chong replay (ts) cho
lenh xuong: MQTT BAT BUOC ky, HTTP poll chi verify khi lenh co kem "sig".

Dung EdgeClient THAT (api_key doc tu Store sqlite that, clock_offset_s that) -
chi thay next_command/ack_command bang fake de khong goi mang."""
import json
import time
from unittest.mock import Mock

import pytest

import node_agent.agent as agent_module
import node_agent.config as config_module
from node_agent.agent import NodeAgent
from node_agent.mqtt_uplink import sign

KEY = "k-downlink-test"  # secret-allow (test fixture)


def _make_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module.settings, "state_dir", tmp_path)
    channels_path = tmp_path / "channels.json"
    channels_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config_module.settings, "channels_file", str(channels_path))
    agent = NodeAgent()
    agent.store.kv_set("api_key", KEY)
    return agent


def _ok_reader():
    reader = Mock()
    reader.command.return_value = {"ok": True}
    return reader


def _signed(cmd, key=KEY, ts="now"):
    body = dict(cmd)
    if ts == "now":
        body["ts"] = int(time.time())
    elif ts is not None:
        body["ts"] = ts
    body["sig"] = sign(key, body)
    return body


def _run(monkeypatch, agent, cmds, mqtt=False, sig_cmd=True):
    """Chay _command_loop() dong bo qua danh sach lenh roi dung. Tra ve list
    ack (cmd_id, ok, detail, kwargs)."""
    acks = []

    def fake_ack(cmd_id, ok, detail="", **kw):
        acks.append((cmd_id, ok, detail, kw))
        return True

    remaining = iter(cmds)
    if mqtt:
        agent.mqtt = Mock()
        # Mock().sig_cmd mac dinh truthy - khai RO de test khong dua vao tinh co
        agent.mqtt.sig_cmd = sig_cmd

        def fake_next(timeout):
            cmd = next(remaining, None)
            if cmd is None:
                agent._stop.set()
            return cmd
        agent.mqtt.next_command.side_effect = fake_next
        agent.mqtt.ack_command.side_effect = fake_ack
    else:
        agent.mqtt = None
        def fake_next_http():
            try:
                return {"command": next(remaining)}
            except StopIteration:
                return {}
        monkeypatch.setattr(agent.client, "next_command", fake_next_http)
        monkeypatch.setattr(agent.client, "ack_command", fake_ack)

        def fake_wait(timeout=None):
            agent._stop.set()
            return False
        monkeypatch.setattr(agent._stop, "wait", fake_wait)

    agent._command_loop()
    return acks


# ----------------------------------------------------------------------
# 1) Lenh hong - khong crash, ack ok:false CHI khi id int hop le

@pytest.mark.parametrize("bad, expected_ack", [
    ("chuoi-khong-phai-object", None),
    ([1, 2, 3], None),
    ({"channel": "a", "cmd": "write"}, None),                         # thieu id
    ({"id": True, "channel": "a", "cmd": "write"}, None),             # id bool
    ({"id": "7", "channel": "a", "cmd": "write"}, None),              # id chuoi
    ({"id": 7, "cmd": "write"}, (7, False, "thieu/sai channel", {})),
    ({"id": 7, "channel": "", "cmd": "write"}, (7, False, "thieu/sai channel", {})),
    ({"id": 7, "channel": "a", "cmd": ""}, (7, False, "thieu/sai cmd", {})),
    ({"id": 7, "channel": "a"}, (7, False, "thieu/sai cmd", {})),
    ({"id": 7, "channel": "a", "cmd": 5}, (7, False, "thieu/sai cmd", {})),
])
@pytest.mark.parametrize("mqtt", [False, True])
def test_malformed_command_does_not_crash_and_loop_continues(monkeypatch, tmp_path, bad, expected_ack, mqtt):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    good = {"id": 50, "channel": "a", "cmd": "write", "value": 1}
    if mqtt:
        good = _signed(good)

    acks = _run(monkeypatch, agent, [bad, good], mqtt=mqtt)

    # lenh hong KHONG duoc thuc thi, lenh hop le ngay sau van chay (loop song)
    reader.command.assert_called_once_with("write", 1, channel="a")
    expected = ([expected_ack] if expected_ack else []) + [(50, True, "", {})]
    assert acks == expected


def test_reader_command_raise_acks_false_continues_and_is_not_remembered(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.side_effect = [RuntimeError("serial boom"), {"ok": True}, {"ok": True}]
    agent._readers = {"a": reader}
    cmd = {"id": 9, "channel": "a", "cmd": "zero"}

    acks = _run(monkeypatch, agent, [dict(cmd), dict(cmd), dict(cmd)])

    # lan 1 raise -> ok:false (khong nho khoa) -> lan 2 retry chay THAT -> lan 3 dedup
    assert reader.command.call_count == 2
    assert acks == [(9, False, "serial boom", {}), (9, True, "", {}), (9, True, "", {})]


# ----------------------------------------------------------------------
# 2) Cua so dedup

def test_dedup_a_b_a_runs_a_only_once(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    a = {"id": 1, "channel": "a", "cmd": "toggle"}
    b = {"id": 2, "channel": "a", "cmd": "toggle"}

    acks = _run(monkeypatch, agent, [dict(a), dict(b), dict(a)])

    assert reader.command.call_count == 2, "A,B,A: A bi chay 2 lan (dao relay 2 lan)"
    assert [x[:2] for x in acks] == [(1, True), (2, True), (1, True)]


def test_dedup_window_holds_exactly_32_keys(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    cmds = [{"id": i, "channel": "a", "cmd": "w"} for i in range(1, 33)]
    cmds.append({"id": 1, "channel": "a", "cmd": "w"})   # van trong cua so 32

    _run(monkeypatch, agent, cmds)

    assert reader.command.call_count == 32


def test_dedup_window_33rd_key_evicts_oldest(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    cmds = [{"id": i, "channel": "a", "cmd": "w"} for i in range(1, 34)]   # 33 khoa
    cmds.append({"id": 2, "channel": "a", "cmd": "w"})   # 2 con trong cua so -> dedup
    cmds.append({"id": 1, "channel": "a", "cmd": "w"})   # 1 da bi day ra -> chay lai

    _run(monkeypatch, agent, cmds)

    assert reader.command.call_count == 34
    assert agent_module._CMD_DEDUP_WINDOW == 32


def test_same_request_id_different_id_is_deduped(monkeypatch, tmp_path):
    """Edge tao lai lenh voi id moi nhung cung request_id (uuid Odoo) -> van
    la CUNG 1 lenh, chi ack lai."""
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    cmds = [
        {"id": 1, "request_id": "R-1", "channel": "a", "cmd": "w"},
        {"id": 2, "request_id": "R-1", "channel": "a", "cmd": "w"},
    ]

    acks = _run(monkeypatch, agent, cmds)

    assert reader.command.call_count == 1
    assert acks == [(1, True, "", {"request_id": "R-1"}), (2, True, "", {"request_id": "R-1"})]


def test_same_id_different_request_id_both_run(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    cmds = [
        {"id": 5, "request_id": "X", "channel": "a", "cmd": "w"},
        {"id": 5, "request_id": "Y", "channel": "a", "cmd": "w"},
    ]

    acks = _run(monkeypatch, agent, cmds)

    assert reader.command.call_count == 2
    assert acks == [(5, True, "", {"request_id": "X"}), (5, True, "", {"request_id": "Y"})]


def test_empty_or_non_string_request_id_falls_back_to_id(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    cmds = [
        {"id": 4, "request_id": "", "channel": "a", "cmd": "w"},
        {"id": 4, "request_id": 123, "channel": "a", "cmd": "w"},   # cung id -> dedup
    ]

    acks = _run(monkeypatch, agent, cmds)

    assert reader.command.call_count == 1
    assert acks == [(4, True, "", {}), (4, True, "", {})]


def test_failed_command_not_remembered_retry_runs_for_real(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.side_effect = [{"ok": False, "error": "busy"}, {"ok": True}]
    agent._readers = {"a": reader}
    cmd = {"id": 11, "request_id": "R-11", "channel": "a", "cmd": "w"}

    acks = _run(monkeypatch, agent, [dict(cmd), dict(cmd), dict(cmd)])

    assert reader.command.call_count == 2
    assert [x[:3] for x in acks] == [(11, False, "busy"), (11, True, ""), (11, True, "")]


def test_unknown_channel_acks_false_and_not_remembered(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    agent._readers = {}
    cmd = {"id": 12, "channel": "zz", "cmd": "w"}

    acks = _run(monkeypatch, agent, [dict(cmd), dict(cmd)])

    assert [x[:2] for x in acks] == [(12, False), (12, False)]
    assert "zz" in acks[0][2]


# ----------------------------------------------------------------------
# 3) Auth nhanh MQTT (BAT BUOC ky)

def _mqtt_reject(monkeypatch, tmp_path, cmd, offset=0.0):
    agent = _make_agent(monkeypatch, tmp_path)
    agent.client.clock_offset_s = offset
    reader = _ok_reader()
    agent._readers = {"a": reader}
    acks = _run(monkeypatch, agent, [cmd], mqtt=True)
    return reader, acks


BASE = {"id": 70, "request_id": "R-70", "channel": "a", "cmd": "w", "value": 1}


def test_mqtt_valid_signed_command_runs(monkeypatch, tmp_path):
    reader, acks = _mqtt_reject(monkeypatch, tmp_path, _signed(BASE))

    reader.command.assert_called_once_with("w", 1, channel="a")
    assert acks == [(70, True, "", {"request_id": "R-70"})]


def _tampered():
    c = _signed(BASE)
    c["value"] = 2
    return c


def _sig_not_str():
    c = _signed(BASE)
    c["sig"] = 123
    return c


@pytest.mark.parametrize("cmd", [
    pytest.param(dict(BASE, ts=int(time.time())), id="thieu-sig"),
    pytest.param(dict(BASE, ts=int(time.time()), sig="0" * 64), id="sai-sig"),
    pytest.param(_tampered(), id="doi-field-sau-ky"),
    pytest.param(_signed(BASE, key="khoa-khac"), id="ky-bang-khoa-khac"),
    pytest.param(_sig_not_str(), id="sig-khong-phai-chuoi"),
])
def test_mqtt_bad_or_missing_sig_rejected_sig_invalid(monkeypatch, tmp_path, cmd):
    reader, acks = _mqtt_reject(monkeypatch, tmp_path, cmd)

    reader.command.assert_not_called()
    assert acks == [(70, False, "sig_invalid", {"request_id": "R-70"})]


def test_mqtt_no_api_key_learned_rejects_sig_invalid(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    agent.store.kv_set("api_key", "")
    reader = _ok_reader()
    agent._readers = {"a": reader}

    acks = _run(monkeypatch, agent, [_signed(BASE)], mqtt=True)

    reader.command.assert_not_called()
    assert acks[0][:3] == (70, False, "sig_invalid")


@pytest.mark.parametrize("ts", [
    pytest.param(None, id="thieu-ts"),
    pytest.param("old", id="ts-qua-khu-130s"),
    pytest.param("future", id="ts-tuong-lai-130s"),
    pytest.param(True, id="ts-bool"),
    pytest.param("12345", id="ts-chuoi"),
])
def test_mqtt_stale_or_missing_ts_rejected(monkeypatch, tmp_path, ts):
    now = int(time.time())
    real_ts = {"old": now - 130, "future": now + 130}.get(ts, ts) if isinstance(ts, str) else ts
    cmd = _signed(BASE, ts=real_ts)

    reader, acks = _mqtt_reject(monkeypatch, tmp_path, cmd)

    reader.command.assert_not_called()
    assert acks == [(70, False, "stale", {"request_id": "R-70"})]


def test_mqtt_ts_within_skew_accepted(monkeypatch, tmp_path):
    reader, acks = _mqtt_reject(monkeypatch, tmp_path, _signed(BASE, ts=int(time.time()) - 110))

    reader.command.assert_called_once()
    assert acks[0][:2] == (70, True)


def test_mqtt_skew_boundary_exactly_120s_with_frozen_clock(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    monkeypatch.setattr(agent_module.time, "time", lambda: 1_000_000.0)
    c_ok = _signed(dict(BASE, id=71, request_id="R-71"), ts=1_000_000 - 120)
    c_bad = _signed(dict(BASE, id=72, request_id="R-72"), ts=1_000_000 - 121)

    acks = _run(monkeypatch, agent, [c_ok, c_bad], mqtt=True)

    assert [x[:3] for x in acks] == [(71, True, ""), (72, False, "stale")]


def test_mqtt_clock_offset_compensates_edge_clock(monkeypatch, tmp_path):
    now = int(time.time())
    # gio edge nhanh hon node 1000s: ts theo gio edge phai duoc chap nhan
    reader, acks = _mqtt_reject(monkeypatch, tmp_path, _signed(BASE, ts=now + 1000), offset=1000.0)
    assert acks[0][:3] == (70, True, "")

    # ...con ts theo gio node (khong bu) thi bi coi la stale
    reader2, acks2 = _mqtt_reject(monkeypatch, tmp_path, _signed(BASE, ts=now), offset=1000.0)
    reader2.command.assert_not_called()
    assert acks2[0][:3] == (70, False, "stale")


def test_mqtt_auth_runs_before_dedup_replay_with_bad_sig_not_acked_ok(monkeypatch, tmp_path):
    """Thu tu shape -> auth -> dedup: lenh gia mao trung khoa voi lenh da chay
    KHONG duoc ack ok:true (khong lo 'da chay' cho ke gia mao)."""
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    forged = dict(BASE, ts=int(time.time()), sig="f" * 64)

    acks = _run(monkeypatch, agent, [_signed(BASE), forged], mqtt=True)

    assert reader.command.call_count == 1
    assert [x[:3] for x in acks] == [(70, True, ""), (70, False, "sig_invalid")]


# ----------------------------------------------------------------------
# 4) Nhanh HTTP poll - chi verify khi CO "sig"

def test_http_unsigned_command_runs_normally(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}

    acks = _run(monkeypatch, agent, [{"id": 80, "channel": "a", "cmd": "w"}])

    reader.command.assert_called_once()
    assert acks == [(80, True, "", {})]


def test_http_command_with_bad_sig_rejected(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    cmd = {"id": 81, "channel": "a", "cmd": "w", "ts": int(time.time()), "sig": "a" * 64}

    acks = _run(monkeypatch, agent, [cmd])

    reader.command.assert_not_called()
    assert acks == [(81, False, "sig_invalid", {})]


def test_http_command_with_valid_sig_runs_and_stale_rejected(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    good = _signed({"id": 82, "channel": "a", "cmd": "w"})
    stale = _signed({"id": 83, "channel": "a", "cmd": "w"}, ts=int(time.time()) - 500)

    acks = _run(monkeypatch, agent, [good, stale])

    assert reader.command.call_count == 1
    assert acks == [(82, True, "", {}), (83, False, "stale", {})]


def test_http_ack_command_echoes_request_id_in_body(monkeypatch, tmp_path):
    """Qua EdgeClient.ack_command THAT: body POST /node/v1/commands/ack co
    request_id khi lenh co, va dung 3 field cu khi khong co."""
    agent = _make_agent(monkeypatch, tmp_path)
    agent.mqtt = None
    reader = _ok_reader()
    agent._readers = {"a": reader}
    posted = []
    resp = Mock(ok=True)
    resp.json.return_value = {"ok": True}
    agent.client.session = Mock()
    agent.client.session.post.side_effect = lambda url, json=None, **kw: (posted.append((url, json)), resp)[1]
    remaining = iter([
        {"id": 90, "request_id": "R-90", "channel": "a", "cmd": "w"},
        {"id": 91, "channel": "a", "cmd": "w"},
    ])

    def fake_next():
        try:
            return {"command": next(remaining)}
        except StopIteration:
            return {}
    monkeypatch.setattr(agent.client, "next_command", fake_next)

    def fake_wait(timeout=None):
        agent._stop.set()
        return False
    monkeypatch.setattr(agent._stop, "wait", fake_wait)

    agent._command_loop()

    assert [p[0].endswith("/node/v1/commands/ack") for p in posted] == [True, True]
    assert posted[0][1] == {"id": 90, "ok": True, "detail": "", "request_id": "R-90"}
    assert posted[1][1] == {"id": 91, "ok": True, "detail": ""}


# ----------------------------------------------------------------------
# 5) Vong 2 (2026-10-02): MQTT chi BAT BUOC ky khi mqtt.sig_cmd True (da khai
#    trong status luc connect); _echo_rid chi echo rid str dai 1..64; nhanh
#    lenh sai hinh dang cung echo rid.

def test_mqtt_sig_cmd_false_accepts_unsigned_command(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}

    acks = _run(monkeypatch, agent, [dict(BASE)], mqtt=True, sig_cmd=False)

    reader.command.assert_called_once_with("w", 1, channel="a")
    assert acks == [(70, True, "", {"request_id": "R-70"})]


def test_mqtt_sig_cmd_true_rejects_unsigned_command(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}

    acks = _run(monkeypatch, agent, [dict(BASE)], mqtt=True, sig_cmd=True)

    reader.command.assert_not_called()
    assert acks == [(70, False, "sig_invalid", {"request_id": "R-70"})]


def test_mqtt_sig_cmd_false_still_verifies_when_sig_present(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    bad = dict(BASE, ts=int(time.time()), sig="0" * 64)
    good = _signed(dict(BASE, id=71, request_id="R-71"))
    stale = _signed(dict(BASE, id=72, request_id="R-72"), ts=int(time.time()) - 500)

    acks = _run(monkeypatch, agent, [bad, good, stale], mqtt=True, sig_cmd=False)

    assert reader.command.call_count == 1
    assert [x[:3] for x in acks] == [(70, False, "sig_invalid"), (71, True, ""), (72, False, "stale")]


def test_mqtt_sig_cmd_read_from_real_uplink_attribute_default_false(monkeypatch, tmp_path):
    """Doi tuong MqttUplink THAT (khong Mock) mac dinh sig_cmd=False truoc
    _on_connect -> lenh khong ky van duoc chay."""
    import node_agent.mqtt_uplink as mu
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    monkeypatch.setattr(mu.mqtt, "Client", lambda *a, **kw: Mock())
    up = mu.MqttUplink(agent.store)
    assert up.sig_cmd is False
    remaining = iter([dict(BASE)])

    def fake_next(timeout):
        cmd = next(remaining, None)
        if cmd is None:
            agent._stop.set()
        return cmd
    acks = []
    monkeypatch.setattr(up, "next_command", fake_next)
    monkeypatch.setattr(up, "ack_command", lambda i, ok, d="", **kw: acks.append((i, ok, d, kw)))
    agent.mqtt = up

    agent._command_loop()

    reader.command.assert_called_once()
    assert acks == [(70, True, "", {"request_id": "R-70"})]


@pytest.mark.parametrize("mqtt", [False, True])
def test_request_id_longer_than_64_not_echoed_but_still_dedup_key(monkeypatch, tmp_path, mqtt):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    long_rid = "r" * 65
    c1 = {"id": 1, "request_id": long_rid, "channel": "a", "cmd": "w"}
    c2 = {"id": 2, "request_id": long_rid, "channel": "a", "cmd": "w"}   # cung rid -> dedup
    if mqtt:
        c1, c2 = _signed(c1), _signed(c2)

    acks = _run(monkeypatch, agent, [c1, c2], mqtt=mqtt)

    assert reader.command.call_count == 1
    assert acks == [(1, True, "", {}), (2, True, "", {})]


def test_request_id_exactly_64_is_echoed(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    agent._readers = {"a": _ok_reader()}
    rid = "r" * 64

    acks = _run(monkeypatch, agent, [{"id": 3, "request_id": rid, "channel": "a", "cmd": "w"}])

    assert acks == [(3, True, "", {"request_id": rid})]


@pytest.mark.parametrize("mqtt", [False, True])
def test_shape_reject_echoes_request_id(monkeypatch, tmp_path, mqtt):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = _ok_reader()
    agent._readers = {"a": reader}
    bad = {"id": 7, "request_id": "R-7", "cmd": "w"}               # thieu channel
    bad_long = {"id": 8, "request_id": "x" * 65, "channel": "a"}   # thieu cmd, rid qua dai

    acks = _run(monkeypatch, agent, [bad, bad_long], mqtt=mqtt)

    reader.command.assert_not_called()
    assert acks == [(7, False, "thieu/sai channel", {"request_id": "R-7"}),
                    (8, False, "thieu/sai cmd", {})]



# ----------------------------------------------------------------------
# 6) Vong 3: _hello_loop khai sig_cmd giua phien khi key hoc SAU luc connect
#    (regression finding reviewer: ca phien MQTT nhan lenh KHONG ky)

def _hello_once(monkeypatch, agent, hello_res, learn_key=None):
    def fake_hello():
        if learn_key:
            agent.store.kv_set("api_key", learn_key)
        return hello_res
    monkeypatch.setattr(agent.client, "hello", fake_hello)

    def fake_wait(timeout=None):
        agent._stop.set()
        return False
    monkeypatch.setattr(agent._stop, "wait", fake_wait)
    agent._hello_loop()


def _hello_agent(monkeypatch, tmp_path, key, sig_cmd, started):
    agent = _make_agent(monkeypatch, tmp_path)
    agent.store.kv_set("api_key", key)
    agent.mqtt = Mock()
    agent.mqtt.sig_cmd = sig_cmd
    if started:
        agent._mqtt_started.set()
    return agent


def test_hello_loop_learns_key_mid_session_announces_sig_cmd(monkeypatch, tmp_path):
    agent = _hello_agent(monkeypatch, tmp_path, key="", sig_cmd=False, started=True)

    _hello_once(monkeypatch, agent, {"ok": True}, learn_key=KEY)

    agent.mqtt.announce_sig_cmd.assert_called_once_with()
    agent.mqtt.start.assert_not_called()


def test_hello_loop_already_sig_cmd_does_not_announce(monkeypatch, tmp_path):
    agent = _hello_agent(monkeypatch, tmp_path, key=KEY, sig_cmd=True, started=True)

    _hello_once(monkeypatch, agent, {"ok": True})

    agent.mqtt.announce_sig_cmd.assert_not_called()


def test_hello_loop_no_key_yet_does_not_announce(monkeypatch, tmp_path):
    agent = _hello_agent(monkeypatch, tmp_path, key="", sig_cmd=False, started=True)

    _hello_once(monkeypatch, agent, {"ok": True, "api_key": None})

    agent.mqtt.announce_sig_cmd.assert_not_called()


def test_hello_loop_failed_hello_does_not_announce(monkeypatch, tmp_path):
    agent = _hello_agent(monkeypatch, tmp_path, key=KEY, sig_cmd=False, started=True)

    _hello_once(monkeypatch, agent, {"ok": False, "error": "down"})

    agent.mqtt.announce_sig_cmd.assert_not_called()


def test_hello_loop_first_hello_starts_mqtt_not_announce(monkeypatch, tmp_path):
    agent = _hello_agent(monkeypatch, tmp_path, key=KEY, sig_cmd=False, started=False)

    _hello_once(monkeypatch, agent, {"ok": True})

    agent.mqtt.start.assert_called_once_with()
    agent.mqtt.announce_sig_cmd.assert_not_called()
    assert agent._mqtt_started.is_set()


def test_hello_loop_http_mode_no_mqtt_no_error(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    agent.mqtt = None

    _hello_once(monkeypatch, agent, {"ok": True})   # khong raise


def test_end_to_end_after_mid_session_announce_unsigned_command_rejected(monkeypatch, tmp_path):
    """MqttUplink THAT: connect luc chua co key (sig_cmd False) -> hello hoc
    key -> _hello_loop announce (PUBACK ok) -> tu do lenh KHONG ky bi tu choi,
    lenh ky dung van chay."""
    import threading
    import paho.mqtt.client as paho
    import node_agent.mqtt_uplink as mu

    agent = _make_agent(monkeypatch, tmp_path)
    agent.store.kv_set("api_key", "")
    published = []

    def fake_publish(topic, payload=None, qos=0, retain=False):
        published.append((topic, payload, retain))
        info = paho.MQTTMessageInfo(len(published))
        info.rc = paho.MQTT_ERR_SUCCESS
        threading.Timer(0.01, info._set_as_published).start()
        return info
    cli = Mock()
    cli.publish.side_effect = fake_publish
    monkeypatch.setattr(mu.mqtt, "Client", lambda *a, **kw: cli)
    up = mu.MqttUplink(agent.store)
    up._on_connect(cli, None, None, 0)
    assert up.sig_cmd is False
    agent.mqtt = up
    agent._mqtt_started.set()

    _hello_once(monkeypatch, agent, {"ok": True}, learn_key=KEY)
    agent._stop.clear()

    assert up.sig_cmd is True
    last = published[-1][1]
    last_body = json.loads(last.decode() if isinstance(last, bytes) else last)
    assert last_body["sig_cmd"] is True and published[-1][2] is True

    reader = _ok_reader()
    agent._readers = {"a": reader}
    remaining = iter([dict(BASE, ts=int(time.time())), _signed(dict(BASE, id=71, request_id="R-71"))])

    def fake_next(timeout):
        cmd = next(remaining, None)
        if cmd is None:
            agent._stop.set()
        return cmd
    acks = []
    monkeypatch.setattr(up, "next_command", fake_next)
    monkeypatch.setattr(up, "ack_command", lambda i, ok, d="", **kw: acks.append((i, ok, d, kw)))

    agent._command_loop()

    assert reader.command.call_count == 1
    assert acks == [(70, False, "sig_invalid", {"request_id": "R-70"}),
                    (71, True, "", {"request_id": "R-71"})]
