# -*- coding: utf-8 -*-
"""Test pcm-downlink-command muc 4 (phan khong phai GPIO - GPIO o
tests/test_gpio_output.py):
- ModbusReader register_type "coil": validate, _read_once read_coils,
  command on/off/write -> write_coil.
- holding/input khong doi hanh vi (on/off bi tu choi).
- agent._command_loop truyen ms/period_ms CHI khi lenh co.
- settings_api: register_type coil, field direction.
- Moi reader khac nhan **opts khong loi."""
import json
from unittest.mock import MagicMock, Mock

import pytest

import node_agent.config as config_module
import node_agent.settings_api as settings_api
from node_agent.agent import NodeAgent
from node_agent.readers.base import ChannelReader
from node_agent.readers.gpio import GpioReader
from node_agent.readers.modbus import ModbusReader, _shared_clients
from node_agent.readers.mqtt import MqttReader
from node_agent.readers.serial_ascii import SerialReader
from node_agent.readers.sim import SimReader


@pytest.fixture(autouse=True)
def _clear_shared_client_registry():
    _shared_clients.clear()
    yield
    _shared_clients.clear()


def _tcp_cfg(**overrides):
    cfg = {"code": "ch1", "conn_type": "tcp", "host": "10.0.0.5", "tcp_port": 502}
    cfg.update(overrides)
    return cfg


def _coil_reader(**overrides):
    cfg = _tcp_cfg(register_type="coil", **overrides)
    reader = ModbusReader(cfg, emit=Mock())
    reader._client = MagicMock()
    reader._client.write_coil.return_value.isError.return_value = False
    return reader


# ----------------------------------------------------------------------
# 1) ModbusReader.__init__ - register_type

@pytest.mark.parametrize("rtype", ["discrete", "holding_register", "coils", 5])
def test_modbus_invalid_register_type_raises_value_error(rtype):
    with pytest.raises(ValueError):
        ModbusReader(_tcp_cfg(register_type=rtype), emit=Mock())


@pytest.mark.parametrize("rtype", ["holding", "input", "coil"])
def test_modbus_valid_register_type_accepted(rtype):
    reader = ModbusReader(_tcp_cfg(register_type=rtype), emit=Mock())

    assert reader._register_type == rtype


@pytest.mark.parametrize("rtype, expected", [
    ("Coil", "coil"), (" holding", "holding"), ("INPUT ", "input"), (None, "holding"), ("", "holding"),
])
def test_modbus_register_type_normalized(rtype, expected):
    """r2: str(x or "holding").strip().lower()."""
    reader = ModbusReader(_tcp_cfg(register_type=rtype), emit=Mock())

    assert reader._register_type == expected


def test_modbus_register_type_default_is_holding():
    reader = ModbusReader(_tcp_cfg(), emit=Mock())

    assert reader._register_type == "holding"


def test_coil_points_width_forced_to_1_even_if_data_type_32bit():
    reader = ModbusReader(_tcp_cfg(register_type="coil", points=[
        {"code": "r1", "reg_offset": 0, "data_type": "f32"},
        {"code": "r2", "reg_offset": 3, "data_type": "u32"},
    ]), emit=Mock())

    assert all(p["width"] == 1 for p in reader._points)
    assert reader._count == 4


def test_build_readers_drops_modbus_channel_with_invalid_register_type(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module.settings, "state_dir", tmp_path)
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([
        dict(_tcp_cfg(code="bad", register_type="discrete"), mode="modbus"),
        {"code": "badgpio", "mode": "gpio", "pin": 5, "direction": "sideways"},
        {"code": "okgpio", "mode": "gpio", "pin": 6, "direction": "output"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config_module.settings, "channels_file", str(channels_path))
    agent = NodeAgent()

    agent._build_readers()

    assert "bad" not in agent._readers
    assert "badgpio" not in agent._readers
    assert isinstance(agent._readers["okgpio"], GpioReader)


# ----------------------------------------------------------------------
# 2) _read_once coil

def test_coil_read_once_single_point_uses_read_coils():
    reader = _coil_reader(address=8, unit_id=4)
    rr = MagicMock()
    rr.isError.return_value = False
    rr.bits = [True, False, False, False, False, False, False, False]
    reader._client.read_coils.return_value = rr

    values = reader._read_once()

    reader._client.read_coils.assert_called_once_with(8, count=1, device_id=4)
    reader._client.read_holding_registers.assert_not_called()
    assert values == [("ch1", 1.0)]


def test_coil_read_once_multipoint_one_request_values_by_reg_offset():
    reader = _coil_reader(address=10, unit_id=2, points=[
        {"code": "r1", "reg_offset": 0, "scale": 10, "offset": 5},   # scale/offset KHONG ap dung
        {"code": "r2", "reg_offset": 2},
        {"code": "r3", "reg_offset": 1},
    ])
    rr = MagicMock()
    rr.isError.return_value = False
    rr.bits = [False, True, True, False, False, False, False, False]   # pad toi boi 8
    reader._client.read_coils.return_value = rr

    values = reader._read_once()

    reader._client.read_coils.assert_called_once_with(10, count=3, device_id=2)
    assert values == [("r1", 0.0), ("r2", 1.0), ("r3", 1.0)]


def test_coil_read_once_error_raises_io_error():
    reader = _coil_reader()
    rr = MagicMock()
    rr.isError.return_value = True
    reader._client.read_coils.return_value = rr

    with pytest.raises(IOError):
        reader._read_once()


# ----------------------------------------------------------------------
# 3) command coil

@pytest.mark.parametrize("cmd, value, level", [
    ("on", None, True), ("off", None, False),
    ("write", 1, True), ("write", 0, False), ("write", 2.5, True), ("write", -1, True),
    ("write", 0.0, False),
    ("write", None, False),            # r2 regression: coil write None -> TAT
])
def test_coil_command_writes_coil_with_bool(cmd, value, level):
    reader = _coil_reader(address=20, unit_id=7)

    result = reader.command(cmd, value)

    assert result == {"ok": True, "status": "ok"}
    reader._client.write_coil.assert_called_once_with(20, level, device_id=7)
    assert type(reader._client.write_coil.call_args.args[1]) is bool
    reader._client.write_register.assert_not_called()
    reader._client.write_registers.assert_not_called()


def test_coil_command_targets_point_address_plus_reg_offset():
    reader = _coil_reader(address=100, unit_id=3, points=[
        {"code": "r1", "reg_offset": 0}, {"code": "r2", "reg_offset": 5},
    ])

    result = reader.command("on", channel="r2")

    assert result["ok"] is True
    reader._client.write_coil.assert_called_once_with(105, True, device_id=3)


def test_coil_command_unknown_channel_rejected():
    reader = _coil_reader(points=[{"code": "r1", "reg_offset": 0}])

    result = reader.command("on", channel="nope")

    assert result["ok"] is False
    reader._client.write_coil.assert_not_called()


def test_coil_command_is_error_response_returns_ok_false():
    reader = _coil_reader()
    reader._client.write_coil.return_value.isError.return_value = True
    reader._client.write_coil.return_value.__str__ = lambda self: "IllegalAddress"

    result = reader.command("on")

    assert result == {"ok": False, "error": "IllegalAddress"}


def test_coil_command_client_raises_returns_ok_false():
    reader = _coil_reader()
    reader._client.write_coil.side_effect = ConnectionError("bus down")

    result = reader.command("off")

    assert result["ok"] is False


def test_coil_command_not_connected_returns_ok_false():
    reader = ModbusReader(_tcp_cfg(register_type="coil"), emit=Mock())

    result = reader.command("on")

    assert result["ok"] is False


@pytest.mark.parametrize("value", ["x", "on", [1], "1", "0", True, False])
def test_coil_write_non_numeric_value_rejected(value):
    reader = _coil_reader()

    result = reader.command("write", value)

    assert result["ok"] is False
    reader._client.write_coil.assert_not_called()


@pytest.mark.parametrize("cmd, value", [("blink", None), ("toggle", 1), ("zero", None)])
def test_coil_unknown_or_incomplete_command_rejected(cmd, value):
    reader = _coil_reader()

    result = reader.command(cmd, value)

    assert result["ok"] is False
    reader._client.write_coil.assert_not_called()


def test_coil_command_accepts_ms_opts_and_ignores_them():
    reader = _coil_reader(address=1, unit_id=1)

    result = reader.command("on", ms=500, period_ms=200)

    assert result["ok"] is True
    reader._client.write_coil.assert_called_once_with(1, True, device_id=1)


# ----------------------------------------------------------------------
# 4) holding/input khong doi hanh vi

@pytest.mark.parametrize("cmd", ["on", "off", "blink"])
def test_holding_rejects_on_off(cmd):
    reader = ModbusReader(_tcp_cfg(register_type="holding"), emit=Mock())
    reader._client = MagicMock()

    result = reader.command(cmd)

    assert result == {"ok": False, "error": "modbus chi ho tro cmd=write kem value"}
    reader._client.write_coil.assert_not_called()
    reader._client.write_register.assert_not_called()


def test_holding_write_still_writes_register():
    reader = ModbusReader(_tcp_cfg(register_type="holding", address=15, unit_id=2), emit=Mock())
    reader._client = MagicMock()
    reader._client.write_register.return_value.isError.return_value = False

    result = reader.command("write", 123, ms=10)

    assert result == {"ok": True, "status": "ok"}
    reader._client.write_register.assert_called_once_with(15, 123, device_id=2)
    reader._client.write_coil.assert_not_called()


def test_input_register_still_read_only_and_on_rejected():
    reader = ModbusReader(_tcp_cfg(register_type="input"), emit=Mock())
    reader._client = MagicMock()

    assert reader.command("write", 1)["ok"] is False
    assert reader.command("on")["ok"] is False
    reader._client.write_coil.assert_not_called()
    reader._client.write_register.assert_not_called()


# ----------------------------------------------------------------------
# 5) agent._command_loop - truyen ms/period_ms chi khi co

def _make_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module.settings, "state_dir", tmp_path)
    channels_path = tmp_path / "channels.json"
    channels_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config_module.settings, "channels_file", str(channels_path))
    return NodeAgent()


def _run_http(monkeypatch, agent, cmds):
    acks = []
    remaining = iter(cmds)
    agent.mqtt = None

    def fake_next():
        try:
            return {"command": next(remaining)}
        except StopIteration:
            return {}

    def fake_ack(cmd_id, ok, detail="", **kw):
        acks.append((cmd_id, ok, detail))
        return True

    def fake_wait(timeout=None):
        agent._stop.set()
        return False

    monkeypatch.setattr(agent.client, "next_command", fake_next)
    monkeypatch.setattr(agent.client, "ack_command", fake_ack)
    monkeypatch.setattr(agent._stop, "wait", fake_wait)
    agent._command_loop()
    return acks


def test_command_loop_without_ms_calls_reader_old_form(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.return_value = {"ok": True}
    agent._readers = {"a": reader}

    _run_http(monkeypatch, agent, [{"id": 1, "channel": "a", "cmd": "on"}])

    reader.command.assert_called_once_with("on", None, channel="a")


def test_command_loop_ms_and_period_none_not_passed(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.return_value = {"ok": True}
    agent._readers = {"a": reader}

    _run_http(monkeypatch, agent, [{"id": 1, "channel": "a", "cmd": "write", "value": 1,
                                    "ms": None, "period_ms": None}])

    reader.command.assert_called_once_with("write", 1, channel="a")


@pytest.mark.parametrize("extra", [
    {"ms": 500}, {"period_ms": 200}, {"ms": 1000, "period_ms": 250}, {"ms": 0},
])
def test_command_loop_passes_ms_period_when_present(monkeypatch, tmp_path, extra):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.return_value = {"ok": True}
    agent._readers = {"a": reader}
    cmd = {"id": 1, "channel": "a", "cmd": "blink"}
    cmd.update(extra)

    acks = _run_http(monkeypatch, agent, [cmd])

    reader.command.assert_called_once_with("blink", None, channel="a", **extra)
    assert acks == [(1, True, "")]


def test_command_loop_does_not_pass_other_unknown_fields(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path)
    reader = Mock()
    reader.command.return_value = {"ok": True}
    agent._readers = {"a": reader}

    _run_http(monkeypatch, agent, [{"id": 1, "channel": "a", "cmd": "on", "evil": "x", "ms": 5}])

    reader.command.assert_called_once_with("on", None, channel="a", ms=5)


def test_command_loop_end_to_end_gpio_output_on_with_real_reader(monkeypatch, tmp_path):
    from gpiozero import Device
    from gpiozero.pins.mock import MockFactory
    Device.pin_factory = MockFactory()
    try:
        agent = _make_agent(monkeypatch, tmp_path)
        reader = GpioReader({"code": "relay", "pin": 22, "direction": "output"}, emit=Mock())
        reader._connect()
        agent._readers = {"relay": reader}

        acks = _run_http(monkeypatch, agent, [
            {"id": 1, "channel": "relay", "cmd": "on"},
            {"id": 2, "channel": "relay", "cmd": "blink", "period_ms": "x"},
        ])

        assert Device.pin_factory.pin(22).state == 1
        assert acks[0] == (1, True, "")
        assert acks[1][0] == 2 and acks[1][1] is False
        reader._close()
    finally:
        Device.pin_factory.close()
        Device.pin_factory = None


# ----------------------------------------------------------------------
# 6) Reader khac nhan **opts khong loi

def test_base_reader_command_accepts_opts():
    reader = ChannelReader({"code": "c"}, emit=Mock())

    result = reader.command("on", None, channel="c", ms=100, period_ms=200)

    assert result["ok"] is False


def test_sim_reader_command_accepts_opts():
    reader = SimReader({"code": "c"}, emit=Mock())

    assert reader.command("on", None, channel="c", ms=100, period_ms=200) == {"ok": True, "status": "ok"}


def test_serial_reader_command_accepts_opts():
    reader = SerialReader({"code": "c", "cmd_zero": "Z\r\n"}, emit=Mock())

    result = reader.command("zero", None, channel="c", ms=100, period_ms=200)

    assert result["ok"] is False           # chua mo cong - nhung KHONG TypeError


def test_mqtt_reader_command_accepts_opts():
    reader = MqttReader({"code": "c", "topic": "t"}, emit=Mock())

    result = reader.command("on", 1, channel="c", ms=100, period_ms=200)

    assert result["ok"] is False           # chua ket noi - nhung KHONG TypeError


# ----------------------------------------------------------------------
# 7) settings_api

def _modbus_values(**overrides):
    values = {
        "code": "vfd1", "mode": "modbus", "conn_type": "tcp",
        "host": "10.0.0.5", "tcp_port": "502",
        "unit_id": "1", "register_type": "holding", "address": "0",
        "data_type": "u16", "scale": "1", "offset": "0", "modbus_poll_ms": "1000",
    }
    values.update(overrides)
    return values


def _gpio_values(**overrides):
    values = {
        "code": "relay1", "mode": "gpio", "pin": "17", "direction": "output",
        "pull_up": "false", "invert": "true", "bounce_ms": "0", "gpio_poll_ms": "200",
    }
    values.update(overrides)
    return values


def test_settings_modbus_coil_register_type_valid():
    assert settings_api._validate_channel(_modbus_values(register_type="coil"), existing_codes=set()) == {}


def test_settings_modbus_coil_saved_in_json():
    ch = settings_api._channel_to_json(_modbus_values(register_type="coil"))

    assert ch["register_type"] == "coil"


@pytest.mark.parametrize("direction", ["", "input", "output"])
def test_settings_gpio_direction_valid(direction):
    assert settings_api._validate_channel(_gpio_values(direction=direction), existing_codes=set()) == {}


@pytest.mark.parametrize("direction", ["x", "OUT", "in"])
def test_settings_gpio_direction_invalid(direction):
    errors = settings_api._validate_channel(_gpio_values(direction=direction), existing_codes=set())

    assert "direction" in errors


@pytest.mark.parametrize("direction, expected", [("output", "output"), ("input", "input"), ("", "input")])
def test_settings_gpio_channel_to_json_saves_direction(direction, expected):
    ch = settings_api._channel_to_json(_gpio_values(direction=direction))

    assert ch["direction"] == expected
    assert ch["mode"] == "gpio" and ch["pin"] == 17 and ch["invert"] is True


def test_settings_gpio_output_json_builds_output_reader():
    ch = settings_api._channel_to_json(_gpio_values(direction="output"))

    reader = GpioReader(ch, emit=Mock())

    assert reader._output is True


def test_coil_mixed_case_config_reads_and_writes_as_coil():
    reader = ModbusReader(_tcp_cfg(register_type=" Coil "), emit=Mock())
    reader._client = MagicMock()
    reader._client.write_coil.return_value.isError.return_value = False

    assert reader.command("on")["ok"] is True
    reader._client.write_coil.assert_called_once()
    reader._client.write_register.assert_not_called()


# ----------------------------------------------------------------------
# 8) r2 - helper base.onoff_level / duration_ms

from node_agent.readers.base import duration_ms, onoff_level  # noqa: E402


@pytest.mark.parametrize("value, expected", [
    (None, False), (0, False), (0.0, False), (1, True), (-1, True), (2.5, True), (1e-9, True),
    ("1", None), ("0", None), ("", None), (True, None), (False, None), ([1], None), ({}, None),
])
def test_onoff_level(value, expected):
    assert onoff_level(value) is expected


@pytest.mark.parametrize("v, expected", [
    (None, 0), (0, 0), (-5, 0), (-1e20, 0), (float("-inf"), 0), (1, 1), (100, 100),
    (99.9, 99), (2 ** 31 - 1, 2 ** 31 - 1), (2 ** 31, 2 ** 31 - 1), (1e13, 2 ** 31 - 1),
    (float("inf"), 2 ** 31 - 1),
])
def test_duration_ms(v, expected):
    result = duration_ms(v)

    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize("v", ["100", "abc", "", True, False, float("nan"), [1], {}])
def test_duration_ms_rejects_non_number(v):
    with pytest.raises(ValueError):
        duration_ms(v)


def test_command_loop_gpio_write_raise_acks_false_old_timer_survives(monkeypatch, tmp_path):
    """r3: device.on() raise -> _command_loop bat thanh ok:false, hen gio tu
    tat cua lenh truoc VAN tat chan."""
    import time
    agent = _make_agent(monkeypatch, tmp_path)
    reader = GpioReader({"code": "relay", "pin": 22, "direction": "output"}, emit=Mock())
    device = Mock()
    reader._device = device
    agent._readers = {"relay": reader}
    try:
        assert reader.command("on", ms=200)["ok"] is True
        old_timer = reader._timer
        device.on.side_effect = RuntimeError("gpio boom")

        acks = _run_http(monkeypatch, agent, [{"id": 5, "channel": "relay", "cmd": "on", "ms": 9000}])

        assert acks == [(5, False, "gpio boom")]
        assert reader._timer is old_timer
        deadline = time.time() + 1.0
        while time.time() < deadline and not device.off.called:
            time.sleep(0.01)
        device.off.assert_called_once_with()
    finally:
        reader._close()
