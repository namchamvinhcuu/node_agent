# -*- coding: utf-8 -*-
"""Test trang cau hinh web /setup, /setup/channels (settings_api.py) - dung
FastAPI TestClient doc lap voi mot app rong (khong qua lifespan/EdgeAgent
that), tranh goi mang that va tranh os._exit() giet ca tien trinh pytest
(_schedule_restart duoc mock trong fixture `client`). Khong dung .env/
channels.json THAT cua project - moi test tro sang tmp_path qua monkeypatch.
"""
import base64
import importlib
import json
import os
import time

import pytest
from dotenv import load_dotenv
from dotenv.main import dotenv_values
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.testclient import TestClient

import node_agent.config as config_module
import node_agent.settings_api as settings_api
from node_agent.config import settings


# ----------------------------------------------------------------------
# Fixtures + helpers dung chung

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_api, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "setup_token", "")
    restarts = []
    monkeypatch.setattr(settings_api, "_schedule_restart", lambda: restarts.append(True))
    app = FastAPI()
    app.include_router(settings_api.router)
    app.state.restarts = restarts
    return TestClient(app)


def _valid_env_form():
    return {
        "NODE_EDGE_URL": "http://127.0.0.1:8000",
        "NODE_SERIAL": "NODE-TEST-01",
        "NODE_NAME": "Test node",
        "NODE_KIND": "pi",
        "NODE_HELLO_INTERVAL_S": "60",
        "NODE_HEARTBEAT_INTERVAL_S": "30",
        "NODE_SUBMIT_INTERVAL_S": "2",
        "NODE_COMMAND_POLL_INTERVAL_S": "2",
        "NODE_SETUP_TOKEN": "",
    }


def _valid_sim_channel_form(code="scale_wt"):
    return {
        "action": "add", "code": code, "mode": "sim",
        "poll_ms": "500", "center": "412.4", "spread": "0.3",
        "port": "", "baud": "", "data_bits": "", "parity": "", "stop_bits": "",
        "terminator": "", "pattern": "", "value_group": "",
        "stable_group": "", "stable_ok": "", "cmd_zero": "", "cmd_tare": "",
    }


class _FakeRequest:
    """Fake toi thieu cho _check_setup_auth - implementation chi goi
    request.headers.get(...), khong can Request that cua Starlette."""

    def __init__(self, headers=None):
        self.headers = headers or {}


def _basic_auth_header(password, user="anyuser"):  # secret-allow: test fixture, khong phai credential that
    token = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
    return "Basic %s" % token


# ----------------------------------------------------------------------
# 1) _write_env_file

def test_write_env_file_creates_new_file_from_scratch(tmp_path):
    path = tmp_path / ".env"
    assert not path.exists()

    settings_api._write_env_file(path, {"NODE_SERIAL": "NODE-01", "NODE_KIND": "pi"})

    assert path.exists()
    values = dotenv_values(str(path))
    assert values["NODE_SERIAL"] == "NODE-01"
    assert values["NODE_KIND"] == "pi"


def test_write_env_file_overwrites_existing_preserves_other_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment giu nguyen\n"
        "NODE_SERIAL=OLD-SERIAL\n"
        "NODE_UNRELATED=keep-me\n",
        encoding="utf-8",
    )

    settings_api._write_env_file(path, {"NODE_SERIAL": "NEW-SERIAL"})

    content = path.read_text(encoding="utf-8")
    assert "# comment giu nguyen" in content
    assert "NODE_UNRELATED=keep-me" in content
    assert content.count("NODE_SERIAL") == 1
    values = dotenv_values(str(path))
    assert values["NODE_SERIAL"] == "NEW-SERIAL"
    assert values["NODE_UNRELATED"] == "keep-me"


def test_write_env_file_drops_stale_duplicate_key(tmp_path):
    """File san co 2 dong cung key (vd sua tay truoc do) - phai chi con lai
    DUY NHAT 1 dong voi gia tri MOI, tranh dong cu con sot lai phia duoi lam
    dotenv doc nham gia tri cu (cung lop bug da fix o sibling edge_collector,
    xem docstring _write_env_file)."""
    path = tmp_path / ".env"
    path.write_text("NODE_SERIAL=OLD_FIRST\nNODE_NAME=foo\nNODE_SERIAL=OLD_SECOND\n", encoding="utf-8")

    settings_api._write_env_file(path, {"NODE_SERIAL": "NEW_VALUE"})

    content = path.read_text(encoding="utf-8")
    assert content.count("NODE_SERIAL") == 1
    assert dotenv_values(str(path))["NODE_SERIAL"] == "NEW_VALUE"


def test_write_env_file_escapes_quote_and_backslash(tmp_path):
    path = tmp_path / ".env"
    tricky_value = 'He said "hi" and used \\ backslash'

    settings_api._write_env_file(path, {"NODE_NAME": tricky_value})

    assert dotenv_values(str(path))["NODE_NAME"] == tricky_value


# ----------------------------------------------------------------------
# 2) _validate

def _valid_values():
    return {
        "NODE_EDGE_URL": "http://127.0.0.1:8000",
        "NODE_SERIAL": "NODE-01",
        "NODE_NAME": "Test",
        "NODE_KIND": "pi",
        "NODE_HELLO_INTERVAL_S": "60",
        "NODE_HEARTBEAT_INTERVAL_S": "30",
        "NODE_SUBMIT_INTERVAL_S": "2",
        "NODE_COMMAND_POLL_INTERVAL_S": "2",
        "NODE_SETUP_TOKEN": "",
    }


def test_validate_accepts_happy_path_values():
    assert settings_api._validate(_valid_values()) == {}


def test_validate_rejects_url_without_scheme():
    values = _valid_values()
    values["NODE_EDGE_URL"] = "127.0.0.1:8000"
    assert "NODE_EDGE_URL" in settings_api._validate(values)


def test_validate_rejects_empty_serial():
    values = _valid_values()
    values["NODE_SERIAL"] = "   "
    assert "NODE_SERIAL" in settings_api._validate(values)


@pytest.mark.parametrize("key", ["NODE_HELLO_INTERVAL_S", "NODE_HEARTBEAT_INTERVAL_S"])
@pytest.mark.parametrize("bad", ["-1", "0", "abc"])
def test_validate_rejects_invalid_int_interval(key, bad):
    values = _valid_values()
    values[key] = bad
    assert key in settings_api._validate(values)


@pytest.mark.parametrize("key", ["NODE_SUBMIT_INTERVAL_S", "NODE_COMMAND_POLL_INTERVAL_S"])
@pytest.mark.parametrize("bad", ["-1", "0", "abc", "nan", "inf"])
def test_validate_rejects_invalid_float_interval(key, bad):
    values = _valid_values()
    values[key] = bad
    assert key in settings_api._validate(values)


def test_validate_rejects_newline_in_any_value():
    values = _valid_values()
    values["NODE_NAME"] = "line1\nMALICIOUS=1"
    assert "NODE_NAME" in settings_api._validate(values)


# ----------------------------------------------------------------------
# 3) _validate_channel + _channel_to_json

def test_validate_channel_accepts_happy_sim():
    values = {"code": "scale_wt", "mode": "sim", "poll_ms": "500", "center": "412.4", "spread": "0.3"}
    assert settings_api._validate_channel(values, existing_codes=set()) == {}


def test_validate_channel_accepts_happy_serial():
    values = {
        "code": "scale_real", "mode": "serial",
        "port": "/dev/ttyUSB0", "baud": "9600",
        "data_bits": "8", "parity": "none",
        "pattern": r"^(ST|US),\s*(GS|NT),\s*([+-]?\s*[\d.]+)\s*(\w+)$",
    }
    assert settings_api._validate_channel(values, existing_codes=set()) == {}


def test_validate_channel_rejects_duplicate_code():
    values = {"code": "scale_wt", "mode": "sim", "poll_ms": "500", "center": "1", "spread": "0.1"}
    errors = settings_api._validate_channel(values, existing_codes={"scale_wt"})
    assert "code" in errors


def test_validate_channel_rejects_invalid_mode():
    values = {"code": "x", "mode": "bogus"}
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "mode" in errors


def test_validate_channel_rejects_serial_missing_port():
    values = {
        "code": "x", "mode": "serial", "port": "  ",
        "baud": "9600", "data_bits": "8", "parity": "none", "pattern": "^x$",
    }
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "port" in errors


def test_validate_channel_rejects_non_numeric_baud():
    values = {
        "code": "x", "mode": "serial", "port": "/dev/ttyUSB0",
        "baud": "fast", "data_bits": "8", "parity": "none", "pattern": "^x$",
    }
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "baud" in errors


def test_validate_channel_rejects_invalid_data_bits():
    values = {
        "code": "x", "mode": "serial", "port": "/dev/ttyUSB0",
        "baud": "9600", "data_bits": "9", "parity": "none", "pattern": "^x$",
    }
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "data_bits" in errors


def test_validate_channel_rejects_invalid_parity():
    values = {
        "code": "x", "mode": "serial", "port": "/dev/ttyUSB0",
        "baud": "9600", "data_bits": "8", "parity": "xyz", "pattern": "^x$",
    }
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "parity" in errors


def test_validate_channel_rejects_invalid_regex_pattern():
    """Regression finding python-reviewer 2026-09-25: pattern loi (khong bien
    dich duoc) phai bi tu choi NGAY luc validate form, khong duoc lot xuong
    channels.json - _build_readers() (agent.py) goi re.compile() luc KHOI
    DONG khong bọc try/except, pattern loi se lam ca process crash-loop vinh
    vien (Docker `restart: always` cu khoi dong lai voi dung config loi do)."""
    values = {
        "code": "x", "mode": "serial", "port": "/dev/ttyUSB0",
        "baud": "9600", "data_bits": "8", "parity": "none",
        "pattern": "(unclosed",
    }
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "pattern" in errors
    assert "Invalid regex" in errors["pattern"][0]


@pytest.mark.parametrize("key", ["value_group", "stop_bits", "stable_group"])
def test_validate_channel_rejects_non_integer_optional_numeric_fields(key):
    """value_group/stop_bits/stable_group deu duoc int() truc tiep trong
    _channel_to_json - truoc day validate khong kiem tra truoc, gia tri sai
    se crash HTTP 500 cau thay vi tra loi form ro rang (finding
    python-reviewer 2026-09-25)."""
    values = {
        "code": "x", "mode": "serial", "port": "/dev/ttyUSB0",
        "baud": "9600", "data_bits": "8", "parity": "none", "pattern": "^x$",
        key: "abc",
    }
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert key in errors


def test_channel_to_json_sim_matches_example_shape():
    values = {"code": "scale_wt", "mode": "sim", "poll_ms": "500", "center": "412.4", "spread": "0.3"}
    ch = settings_api._channel_to_json(values)
    assert ch == {
        "code": "scale_wt", "mode": "sim",
        "poll_ms": 500, "center": 412.4, "spread": 0.3,
    }


def test_channel_to_json_serial_converts_literal_terminator_to_real_crlf_bytes():
    """Gia tri terminator tu form la CHUOI LITERAL 4 ky tu (backslash-r-
    backslash-n, dung nhu default hien trong input HTML) - _channel_to_json
    phai tra ve ky tu CR/LF THAT (2 byte), khong phai giu nguyen chuoi
    literal 4 ky tu - neu khong reader se gui sai byte xuong thiet bi that.
    Da tung tu verify thuc nghiem 1 lan (yeu cau task) - co dinh lai bang
    test tu dong de khong regress."""
    literal_terminator = "\\r\\n"
    assert len(literal_terminator) == 4  # sanity: dung la chuoi literal 4 ky tu

    values = {
        "code": "scale_real", "mode": "serial", "port": "/dev/ttyUSB0",
        "baud": "9600", "data_bits": "8", "parity": "none", "stop_bits": "1",
        "terminator": literal_terminator,
        "pattern": r"^(ST|US),\s*(GS|NT),\s*([+-]?\s*[\d.]+)\s*(\w+)$",
        "value_group": "3", "stable_group": "1", "stable_ok": "ST",
        "cmd_zero": "Z\\r\\n", "cmd_tare": "T\\r\\n",
    }

    ch = settings_api._channel_to_json(values)

    assert ch["terminator"] == "\r\n"
    assert len(ch["terminator"]) == 2
    assert ch["cmd_zero"] == "Z\r\n"
    assert ch["cmd_tare"] == "T\r\n"
    # Round-trip qua json.dumps/json.load nhu _write_channels()/load_channels()
    # lam that - phai lay lai dung CR/LF that, khop cau truc channels.example.json.
    reloaded = json.loads(json.dumps(ch, ensure_ascii=False))
    assert reloaded == {
        "code": "scale_real", "mode": "serial", "port": "/dev/ttyUSB0",
        "baud": 9600, "data_bits": 8, "parity": "none", "stop_bits": 1,
        "terminator": "\r\n",
        "pattern": r"^(ST|US),\s*(GS|NT),\s*([+-]?\s*[\d.]+)\s*(\w+)$",
        "value_group": 3, "stable_group": 1, "stable_ok": "ST",
        "cmd_zero": "Z\r\n", "cmd_tare": "T\r\n",
    }


# ----------------------------------------------------------------------
# 3b) _validate_channel + _channel_to_json - mode=modbus (reader moi,
# CHUA co thiet bi Modbus that trong moi truong dev - xem
# node_agent/readers/modbus.py va tests/test_modbus_reader.py)

def _valid_modbus_tcp_values(**overrides):
    values = {
        "code": "vfd1", "mode": "modbus", "conn_type": "tcp",
        "host": "10.0.0.5", "tcp_port": "502",
        "unit_id": "1", "register_type": "holding", "address": "0",
        "data_type": "u16", "scale": "1", "offset": "0", "modbus_poll_ms": "1000",
    }
    values.update(overrides)
    return values


def _valid_modbus_rtu_values(**overrides):
    values = {
        "code": "vfd2", "mode": "modbus", "conn_type": "rtu",
        "modbus_port": "/dev/ttyUSB2", "modbus_baud": "19200",
        "unit_id": "1", "register_type": "input", "address": "0",
        "data_type": "u16", "scale": "1", "offset": "0", "modbus_poll_ms": "1000",
    }
    values.update(overrides)
    return values


def test_validate_channel_accepts_happy_modbus_tcp():
    assert settings_api._validate_channel(_valid_modbus_tcp_values(), existing_codes=set()) == {}


def test_validate_channel_accepts_happy_modbus_rtu():
    assert settings_api._validate_channel(_valid_modbus_rtu_values(), existing_codes=set()) == {}


def test_validate_channel_rejects_modbus_tcp_missing_host_and_port():
    values = _valid_modbus_tcp_values(host="  ", tcp_port="")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "host" in errors
    assert "tcp_port" in errors


def test_validate_channel_rejects_modbus_tcp_non_integer_port():
    values = _valid_modbus_tcp_values(tcp_port="not-a-port")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "tcp_port" in errors


def test_validate_channel_rejects_modbus_rtu_missing_port_and_baud():
    values = _valid_modbus_rtu_values(modbus_port="  ", modbus_baud="")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "modbus_port" in errors
    assert "modbus_baud" in errors


def test_validate_channel_rejects_modbus_rtu_non_integer_baud():
    values = _valid_modbus_rtu_values(modbus_baud="fast")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "modbus_baud" in errors


def test_validate_channel_rejects_modbus_invalid_conn_type():
    values = _valid_modbus_tcp_values(conn_type="bogus")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "conn_type" in errors
    # conn_type sai -> khong con biet kiem host/tcp_port hay port/baud, khong
    # duoc bao loi nham vao 2 nhanh do (xem nhanh elif trong _validate_channel).
    assert "host" not in errors
    assert "modbus_port" not in errors


def test_validate_channel_rejects_modbus_invalid_data_type():
    values = _valid_modbus_tcp_values(data_type="u8")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "data_type" in errors


def test_validate_channel_rejects_modbus_invalid_register_type():
    values = _valid_modbus_tcp_values(register_type="coil")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "register_type" in errors


def test_validate_channel_rejects_modbus_negative_address():
    values = _valid_modbus_tcp_values(address="-1")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "address" in errors


def test_validate_channel_rejects_modbus_non_integer_unit_id():
    values = _valid_modbus_tcp_values(unit_id="abc")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "unit_id" in errors


def test_validate_channel_rejects_modbus_non_integer_poll_ms():
    values = _valid_modbus_tcp_values(modbus_poll_ms="abc")
    errors = settings_api._validate_channel(values, existing_codes=set())
    assert "modbus_poll_ms" in errors


def test_channel_to_json_modbus_tcp_maps_fields_correctly():
    values = _valid_modbus_tcp_values(unit_id="3", register_type="holding", address="10",
                                       data_type="f32", scale="2.5", offset="1.0",
                                       modbus_poll_ms="500", host="10.0.0.5", tcp_port="502")
    ch = settings_api._channel_to_json(values)
    assert ch == {
        "code": "vfd1", "mode": "modbus", "conn_type": "tcp",
        "unit_id": 3, "register_type": "holding", "address": 10, "data_type": "f32",
        "scale": 2.5, "offset": 1.0, "poll_ms": 500,
        "host": "10.0.0.5", "tcp_port": 502,
    }
    # KHONG duoc lan sang field cua nhanh rtu.
    assert "port" not in ch
    assert "baud" not in ch


def test_channel_to_json_modbus_rtu_maps_fields_correctly():
    """RTU: modbus_port -> port, modbus_baud -> baud, modbus_poll_ms ->
    poll_ms trong JSON output - day la 3 field UI dat ten khac JSON channel
    that su (tranh trung ten voi "port"/"baud" cua mode=serial trong cung
    form add-channel)."""
    values = _valid_modbus_rtu_values(modbus_port="/dev/ttyUSB2", modbus_baud="19200",
                                       scale="", offset="", modbus_poll_ms="1000")
    ch = settings_api._channel_to_json(values)
    assert ch == {
        "code": "vfd2", "mode": "modbus", "conn_type": "rtu",
        "unit_id": 1, "register_type": "input", "address": 0, "data_type": "u16",
        "scale": 1.0, "offset": 0.0, "poll_ms": 1000,
        "port": "/dev/ttyUSB2", "baud": 19200,
    }
    assert "host" not in ch
    assert "tcp_port" not in ch


# ----------------------------------------------------------------------
# 4) _is_same_origin

def _same_origin_probe_app():
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: FastAPIRequest):
        return {"same_origin": settings_api._is_same_origin(request)}

    return app


def test_is_same_origin_accepts_matching_origin():
    probe_client = TestClient(_same_origin_probe_app())
    resp = probe_client.get("/probe", headers={"origin": "http://testserver"})
    assert resp.json() == {"same_origin": True}


def test_is_same_origin_rejects_mismatched_origin():
    probe_client = TestClient(_same_origin_probe_app())
    resp = probe_client.get("/probe", headers={"origin": "http://evil.example"})
    assert resp.json() == {"same_origin": False}


def test_is_same_origin_allows_when_origin_and_referer_missing():
    """Thiet ke best-effort: khong co Origin lan Referer (client cu/browser
    khong gui) thi CHO QUA, khong tu choi oan."""
    probe_client = TestClient(_same_origin_probe_app())
    resp = probe_client.get("/probe")
    assert resp.json() == {"same_origin": True}


# ----------------------------------------------------------------------
# 5) _check_setup_auth

def test_check_setup_auth_passthrough_when_token_empty(monkeypatch):
    monkeypatch.setattr(settings, "setup_token", "")
    assert settings_api._check_setup_auth(_FakeRequest()) is None


def test_check_setup_auth_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(settings, "setup_token", "sekret")  # secret-allow: test fixture
    request = _FakeRequest({"authorization": _basic_auth_header("sekret")})
    assert settings_api._check_setup_auth(request) is None


def test_check_setup_auth_rejects_wrong_or_missing_token(monkeypatch):
    monkeypatch.setattr(settings, "setup_token", "sekret")  # secret-allow: test fixture

    wrong = settings_api._check_setup_auth(_FakeRequest({"authorization": _basic_auth_header("wrong")}))
    assert wrong is not None and wrong.status_code == 401

    missing = settings_api._check_setup_auth(_FakeRequest())
    assert missing is not None and missing.status_code == 401


def test_check_setup_auth_rejects_non_ascii_password_without_crash(monkeypatch):
    """Regression cho lop bug da gap o sibling edge_collector:
    secrets.compare_digest(str, str) raise TypeError khi 1 trong 2 chuoi chua
    ky tu non-ASCII - phai encode utf-8 sang bytes TRUOC khi so (xem docstring
    _check_setup_auth). Assertion quan trong nhat la 401, KHONG phai exception
    thoat ra ngoai (se thanh 500 o tang HTTP)."""
    monkeypatch.setattr(settings, "setup_token", "sekret")  # secret-allow: test fixture
    request = _FakeRequest({"authorization": _basic_auth_header("héllo")})

    result = settings_api._check_setup_auth(request)

    assert result is not None
    assert result.status_code == 401


# ----------------------------------------------------------------------
# 6) HTTP-level qua FastAPI TestClient

def test_get_setup_returns_200_with_current_values(client, tmp_path):
    (tmp_path / ".env").write_text('NODE_SERIAL="NODE-EXISTING"\n', encoding="utf-8")

    resp = client.get("/setup")

    assert resp.status_code == 200
    assert "NODE-EXISTING" in resp.text


def test_post_setup_valid_values_writes_env_file(client, tmp_path):
    resp = client.post("/setup", data=_valid_env_form())

    assert resp.status_code == 200
    env_path = tmp_path / ".env"
    assert env_path.exists()
    values = dotenv_values(str(env_path))
    assert values["NODE_SERIAL"] == "NODE-TEST-01"
    assert values["NODE_EDGE_URL"] == "http://127.0.0.1:8000"
    assert client.app.state.restarts == [True]  # _schedule_restart duoc goi dung 1 lan


def test_post_setup_rejects_invalid_values_without_writing(client, tmp_path):
    form = _valid_env_form()
    form["NODE_SERIAL"] = ""

    resp = client.post("/setup", data=form)

    assert resp.status_code == 400
    assert not (tmp_path / ".env").exists()


def test_post_setup_rejects_cross_origin_request(client, tmp_path):
    """CSRF best-effort: Origin header CO nhung KHAC voi host request -> tu
    choi 403, khong ghi file."""
    resp = client.post("/setup", data=_valid_env_form(), headers={"origin": "http://evil.example"})

    assert resp.status_code == 403
    assert not (tmp_path / ".env").exists()


def test_post_setup_identical_values_does_not_trigger_restart(client):
    """DoS-guard 2026-09-25: submit lai DUNG gia tri hien tai (khong co gi
    thay doi) khong duoc trigger _schedule_restart/os._exit - Save van thanh
    cong (saved=True), chi la khong can restart vi khong co gi de ap dung."""
    form = _valid_env_form()

    first = client.post("/setup", data=form)
    assert first.status_code == 200
    assert client.app.state.restarts == [True]

    second = client.post("/setup", data=form)

    assert second.status_code == 200
    assert "Saved" in second.text
    assert client.app.state.restarts == [True]  # khong them lan restart nao


def test_channels_post_delete_nonexistent_code_does_not_trigger_restart(client, tmp_path):
    """DoS-guard 2026-09-25 (nhanh delete): xoa 1 code KHONG ton tai -> danh
    sach kenh khong doi, khong duoc trigger restart."""
    (tmp_path / "channels.json").write_text(
        json.dumps([{"code": "scale_wt", "mode": "sim", "poll_ms": 500,
                     "center": 1, "spread": 0.1}]) + "\n",
        encoding="utf-8",
    )

    resp = client.post("/setup/channels", data={"action": "delete", "code": "does_not_exist"})

    assert resp.status_code == 200
    assert client.app.state.restarts == []


def test_channels_post_add_returns_500_with_clear_banner_when_write_fails(client, monkeypatch):
    """Regression finding python-reviewer 2026-09-25: channels_post truoc day
    THIEU try/except quanh _write_channels() (khac voi setup_post da co san) -
    OSError (vd container uid 1000 khong ghi duoc file bind-mount) se la 500
    trang tron cua FastAPI thay vi banner loi ro nguyen nhan. Mo phong bang
    monkeypatch _write_channels de raise PermissionError, on dinh hon chmod
    file that (test co the chay boi root, bo qua permission bit)."""
    def _raise(_channels):
        raise PermissionError("[Errno 13] Permission denied")

    monkeypatch.setattr(settings_api, "_write_channels", _raise)

    resp = client.post("/setup/channels", data=_valid_sim_channel_form(code="new_ch"))

    assert resp.status_code == 500
    assert "Could not write channels.json" in resp.text
    assert "Traceback" not in resp.text
    assert client.app.state.restarts == []


def test_channels_post_delete_returns_500_with_clear_banner_when_write_fails(client, monkeypatch, tmp_path):
    """Cung finding tren, ap dung cho nhanh delete (ca 2 nhanh add/delete deu
    duoc boc try/except moi trong diff nay)."""
    (tmp_path / "channels.json").write_text(
        json.dumps([{"code": "scale_wt", "mode": "sim", "poll_ms": 500,
                     "center": 1, "spread": 0.1}]) + "\n",
        encoding="utf-8",
    )

    def _raise(_channels):
        raise PermissionError("[Errno 13] Permission denied")

    monkeypatch.setattr(settings_api, "_write_channels", _raise)

    resp = client.post("/setup/channels", data={"action": "delete", "code": "scale_wt"})

    assert resp.status_code == 500
    assert "Could not write channels.json" in resp.text
    assert client.app.state.restarts == []


# ----------------------------------------------------------------------
# _schedule_restart cooldown (DoS-guard 2026-09-25)

def test_schedule_restart_respects_cooldown_window(tmp_path, monkeypatch):
    """Goi lai qua gan lan truoc (trong _RESTART_COOLDOWN_S) phai tra False
    va KHONG spawn thread _die (nen KHONG bao gio goi os._exit that) - danh
    dau bang fake Thread class thay vi mock os._exit, tranh rui ro thread
    that ngu 0.5s roi goi os._exit that sau khi monkeypatch da revert (giet
    ca tien trinh pytest).

    Cooldown gio persist qua FILE marker (mtime cua _restart_marker_path(),
    vong 2 fix python-reviewer 2026-09-25 - bien RAM `_last_restart_at` cu bi
    chinh os._exit() xoa sach moi lan trigger nen vo dung voi kich ban tan
    cong that). monkeypatch `settings.state_dir` sang tmp_path de marker
    khong dung that ./var/last_setup_restart cua project.

    KHONG mock time.time(): marker.touch() ghi mtime bang dong ho THAT cua
    OS (khong bi anh huong boi monkeypatch time.time trong Python) - mock
    time.time() se lam `now` (gia) va `last=marker.stat().st_mtime` (that)
    lech nhau ca ty giay, sai lech logic cooldown. De gia lap "marker da
    cu" mot cach xac dinh (khong can sleep that), lui THAT mtime cua marker
    ve qua khu bang os.utime()."""
    monkeypatch.setattr(settings, "state_dir", tmp_path)
    thread_starts = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            thread_starts.append(self._target)

    monkeypatch.setattr(settings_api.threading, "Thread", _FakeThread)

    marker_path = tmp_path / "last_setup_restart"

    # 1) Chua co marker -> luon duoc phep, tao marker moi.
    assert settings_api._schedule_restart() is True
    assert len(thread_starts) == 1
    assert marker_path.exists()

    # 2) Goi lai ngay lap tuc (marker con rat moi) -> phai bi chan.
    assert settings_api._schedule_restart() is False
    assert len(thread_starts) == 1  # khong spawn them thread nao

    # 3) Lui THAT mtime cua marker ve qua han cooldown -> phai duoc phep lai.
    old_ts = time.time() - settings_api._RESTART_COOLDOWN_S - 1
    os.utime(marker_path, (old_ts, old_ts))
    assert settings_api._schedule_restart() is True
    assert len(thread_starts) == 2


def test_schedule_restart_cooldown_survives_module_reload_simulating_process_restart(tmp_path, monkeypatch):
    """Bang chung PHAN BIET voi bien RAM cu (`_last_restart_at`, da bi xoa
    hoan toan khoi code o vong 2 fix python-reviewer 2026-09-25): reload lai
    MODULE settings_api (importlib.reload) ngay sau khi marker vua duoc tao -
    mo phong dung kich ban that (os._exit() giet tien trinh, Docker
    `restart: always` khoi dong lai VOI TIEN TRINH PYTHON HOAN TOAN MOI, moi
    bien global duoc khoi tao lai tu dau). Neu van dung bien RAM, "tien trinh
    moi" nay se thay `_last_restart_at == 0.0` (gia tri khoi tao) va cho qua
    NGAY LAP TUC - khong chan duoc 1 request lien tiep ngay sau khi container
    vua len lai (chinh la finding vong 2 cua python-reviewer). Marker la FILE
    tren dia (khong bi xoa boi reload/process chet) nen van phai BI CHAN."""
    monkeypatch.setattr(settings, "state_dir", tmp_path)

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            pass

        def start(self):
            pass  # khong chay _die that (se sleep 0.5s roi os._exit that)

    monkeypatch.setattr(settings_api.threading, "Thread", _FakeThread)

    assert settings_api._schedule_restart() is True  # "process cu" tao marker

    try:
        importlib.reload(settings_api)  # mo phong process MOI (import lai tu dau)

        # "process moi" goi lai NGAY SAU restart - marker van con moi
        # (< _RESTART_COOLDOWN_S), phai van bi chan du la lan goi DAU TIEN
        # tren "instance" module nay.
        assert settings_api._schedule_restart() is False
    finally:
        importlib.reload(settings_api)  # dam bao module o trang thai sach cho test sau


# ----------------------------------------------------------------------
# Finding Critical 2026-09-25: Docker env_file: chi bom os.environ 1 lan luc
# container CREATE (khong phai moi lan restart) - load_dotenv(override=True)
# la co che fix that su. Test truc tiep hanh vi override=True cua python-dotenv
# thay vi reload toan bo module `config` (import-time side-effect kho reset
# giua cac test - settings_api da `from .config import DOTENV_PATH, settings`
# luc import, reload rieng `config` khong cap nhat lai binding do).

def test_env_path_shared_with_config_dotenv_path():
    """settings_api._ENV_PATH phai la CUNG mot Path voi config.DOTENV_PATH -
    xem comment config.py: '.env CHIA SE voi settings_api.py, khong de moi
    noi tu lay path rieng se lech nhau tuy CWD luc start process'. Test nay
    KHONG dùng fixture `client` (khong monkeypatch _ENV_PATH) de doc dung
    binding that luc import."""
    assert settings_api._ENV_PATH == config_module.DOTENV_PATH


def test_load_dotenv_override_true_lets_new_env_file_value_win_over_stale_os_environ(tmp_path, monkeypatch):
    """Regression cho finding Critical python-reviewer 2026-09-25: Docker
    `env_file:` bom NODE_HELLO_INTERVAL_S vao os.environ CHI 1 LAN duy nhat
    luc container duoc TAO (docker create/run), KHONG phai moi lan container
    KHOI DONG LAI (restart). Sau khi web UI ghi .env MOI (bind-mount) roi
    _schedule_restart() (os._exit + Docker `restart: always`), tien trinh moi
    khoi dong lai VAN LA CUNG container - os.environ da mang san gia tri CU
    tu luc create. Neu load_dotenv() dung mac dinh (override=False), gia tri
    MOI trong .env se KHONG BAO GIO co hieu luc (Save tren web UI "khong lam
    gi ca" sau moi restart) - day chinh la co che fix that su trong
    config.py: `load_dotenv(DOTENV_PATH, override=True)`."""
    monkeypatch.setenv("NODE_HELLO_INTERVAL_S", "999")  # os.environ da co san gia tri CU (Docker env_file: luc create)
    env_path = tmp_path / ".env"
    env_path.write_text("NODE_HELLO_INTERVAL_S=45\n", encoding="utf-8")  # gia tri MOI vua Save qua web UI

    load_dotenv(env_path, override=True)

    assert os.environ["NODE_HELLO_INTERVAL_S"] == "45"


def test_load_dotenv_default_override_false_would_reproduce_stale_bug(tmp_path, monkeypatch):
    """Doi chung lam ro TAI SAO can override=True (KHONG phai code dang chay
    that - la tai lieu song chung minh dung co che bug CU truoc fix, de
    regression neu ai do lo sua config.py bo mat `override=True` se duoc bat
    boi chinh test tren, con test nay giai thich VI SAO no se sai)."""
    monkeypatch.setenv("NODE_HELLO_INTERVAL_S", "999")
    env_path = tmp_path / ".env"
    env_path.write_text("NODE_HELLO_INTERVAL_S=45\n", encoding="utf-8")

    load_dotenv(env_path)  # override=False mac dinh

    assert os.environ["NODE_HELLO_INTERVAL_S"] == "999"  # gia tri CU bi ket, dung bug da gap


# ----------------------------------------------------------------------
# 8) _scan_serial_ports / GET /setup/channels/scan_ports

def test_scan_serial_ports_finds_matching_dev_nodes(monkeypatch):
    """Goi dung ham that _scan_serial_ports(), chi mock glob.glob (khong
    dung /dev that cua may chay test) - tra file gia theo dung 3 pattern ham
    dang quet, xac nhan gop + sap xep dung, khong trung lap."""
    fake = {
        "/dev/ttyUSB*": ["/dev/ttyUSB0"],
        "/dev/ttyACM*": ["/dev/ttyACM0", "/dev/ttyACM0"],  # trung lap co y - phai bi loai qua set()
        "/dev/ttyAMA*": [],
    }
    monkeypatch.setattr(settings_api.glob, "glob", lambda pat: fake.get(pat, []))

    result = settings_api._scan_serial_ports()

    assert result == ["/dev/ttyACM0", "/dev/ttyUSB0"]


def test_scan_serial_ports_empty_when_no_device(monkeypatch):
    monkeypatch.setattr(settings_api.glob, "glob", lambda pat: [])

    assert settings_api._scan_serial_ports() == []


def test_get_scan_ports_returns_200_json_list(client, monkeypatch):
    monkeypatch.setattr(settings_api, "_scan_serial_ports", lambda: ["/dev/ttyUSB0", "/dev/ttyACM0"])

    resp = client.get("/setup/channels/scan_ports")

    assert resp.status_code == 200
    assert resp.json() == {"ports": ["/dev/ttyUSB0", "/dev/ttyACM0"]}


def test_get_scan_ports_requires_auth_when_token_set(client, monkeypatch):
    monkeypatch.setattr(settings, "setup_token", "s3cr3t")

    resp = client.get("/setup/channels/scan_ports")

    assert resp.status_code == 401


def test_root_redirects_to_setup(client):
    resp = client.get("/", follow_redirects=False)

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/setup"


def test_port_field_html_no_error_still_renders_scan_button_and_input(monkeypatch):
    """errors=={} (truong hop GET /setup/channels binh thuong, chua submit
    gi) - phai co input + nut Scan, KHONG co the <p class="err">."""
    out = settings_api._port_field_html({})

    assert 'onclick="scanPorts()"' in out
    assert 'id="port-input"' in out
    assert 'class="err"' not in out


def test_port_field_html_renders_error_message_when_present():
    out = settings_api._port_field_html({"port": ["Khong duoc de trong"]})

    assert '<p class="err">Khong duoc de trong</p>' in out
    # Loi khac (vd baud) khong duoc lan sang field port.
    assert out.count('class="err"') == 1


def test_channels_post_add_serial_missing_port_shows_error_and_scan_button(client):
    """Tich hop that qua HTTP: POST /setup/channels thieu port -> _render_channels
    phai di qua _port_field_html(errors) va hien dung loi (khong phai field()
    generic cu, vi field port da doi sang scan-button UI)."""
    form = {
        "action": "add", "code": "scale_wt", "mode": "serial",
        "poll_ms": "", "center": "", "spread": "",
        "port": "", "baud": "9600", "data_bits": "8", "parity": "none", "stop_bits": "1",
        "terminator": "", "pattern": "^x$", "value_group": "1",
        "stable_group": "", "stable_ok": "", "cmd_zero": "", "cmd_tare": "",
    }

    resp = client.post("/setup/channels", data=form)

    assert resp.status_code == 400
    assert "Must not be blank" in resp.text


# ----------------------------------------------------------------------
# 7) Approach B - action=add_point (them 1 diem vao nguon Modbus da co) va
#    delete/list hien thi dung 1 dong/point - xem docstring dau file
#    node_agent/readers/modbus.py va settings_api.py::_validate_point/
#    _all_codes.

def _single_point_modbus_channel(code="vfd1", **overrides):
    """1 nguon Modbus DON diem (KHONG co "points") - dung shape THAT ma
    _channel_to_json() sinh ra cho mode=modbus/conn_type=tcp (xem
    test_channel_to_json_modbus_tcp_maps_fields_correctly)."""
    ch = {
        "code": code, "mode": "modbus", "conn_type": "tcp",
        "unit_id": 1, "register_type": "holding", "address": 0,
        "data_type": "i16", "scale": 2.0, "offset": 5.0, "poll_ms": 1000,
        "host": "10.0.0.5", "tcp_port": 502,
    }
    ch.update(overrides)
    return ch


def _multipoint_modbus_channel(code="src1"):
    return {
        "code": code, "mode": "modbus", "conn_type": "tcp",
        "unit_id": 1, "register_type": "holding", "address": 0, "poll_ms": 1000,
        "host": "10.0.0.5", "tcp_port": 502,
        "points": [
            {"code": "a", "reg_offset": 0, "data_type": "u16", "scale": 1.0, "offset": 0.0},
            {"code": "b", "reg_offset": 1, "data_type": "u16", "scale": 1.0, "offset": 0.0},
        ],
    }


def _add_point_form(**overrides):
    form = {
        "action": "add_point", "point_source_code": "vfd1", "point_code": "vfd1_p2",
        "point_reg_offset": "1", "point_data_type": "u16",
        "point_scale": "1", "point_offset": "0",
    }
    form.update(overrides)
    return form


def test_add_point_converts_single_point_channel_to_multipoint_source(client, tmp_path):
    """Them point dau tien vao 1 nguon Modbus con la don-diem -> nguon do
    phai duoc CHUYEN thanh multi-point, point[0] giu DUNG data_type/scale/
    offset GOC cua chinh channel cu (khong duoc mat/sai lech du lieu)."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([_single_point_modbus_channel()]), encoding="utf-8")

    resp = client.post("/setup/channels", data=_add_point_form())

    assert resp.status_code == 200
    assert client.app.state.restarts == [True]
    saved = json.loads(channels_path.read_text(encoding="utf-8"))
    assert len(saved) == 1
    entry = saved[0]
    assert entry["code"] == "vfd1"
    assert entry["points"] == [
        {"code": "vfd1", "reg_offset": 0, "data_type": "i16", "scale": 2.0, "offset": 5.0},
        {"code": "vfd1_p2", "reg_offset": 1, "data_type": "u16", "scale": 1.0, "offset": 0.0},
    ]
    # unit_id/register_type/address/host/tcp_port/poll_ms cua nguon KHONG doi.
    assert entry["unit_id"] == 1
    assert entry["host"] == "10.0.0.5"


def test_add_point_rejects_duplicate_code_across_nested_points(client, tmp_path):
    """Code moi trung voi 1 code DA NAM TRONG "points" cua 1 nguon khac ->
    bi chan 400 - kiem tra _all_codes() co duyet vao ben trong "points",
    khong chi liet ke code cap cao nhat cua tung channel."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([_multipoint_modbus_channel()]), encoding="utf-8")

    resp = client.post("/setup/channels", data=_add_point_form(point_source_code="src1", point_code="b"))

    assert resp.status_code == 400
    assert "already exists" in resp.text
    assert client.app.state.restarts == []
    # Khong ghi de channels.json khi loi validate.
    saved = json.loads(channels_path.read_text(encoding="utf-8"))
    assert saved == [_multipoint_modbus_channel()]


def test_delete_point_keeps_other_points_in_same_source(client, tmp_path):
    """Xoa 1 point trong nguon co 2 point -> point con lai VAN CON trong
    channels.json, KHONG bi xoa nham ca nguon."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([_multipoint_modbus_channel()]), encoding="utf-8")

    resp = client.post("/setup/channels", data={"action": "delete", "code": "a"})

    assert resp.status_code == 200
    assert client.app.state.restarts == [True]
    saved = json.loads(channels_path.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["code"] == "src1"          # nguon van con
    assert saved[0]["points"] == [
        {"code": "b", "reg_offset": 1, "data_type": "u16", "scale": 1.0, "offset": 0.0},
    ]


def test_delete_last_point_removes_whole_source_entry(client, tmp_path):
    """Nguon chi con 1 point, xoa not point do -> ca entry nguon bien mat
    hoan toan khoi channels.json (khong con "points": [] mo coi)."""
    solo_source = {
        "code": "src2", "mode": "modbus", "conn_type": "tcp",
        "unit_id": 1, "register_type": "holding", "address": 0, "poll_ms": 1000,
        "host": "10.0.0.6", "tcp_port": 502,
        "points": [{"code": "solo", "reg_offset": 0, "data_type": "u16", "scale": 1.0, "offset": 0.0}],
    }
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([solo_source]), encoding="utf-8")

    resp = client.post("/setup/channels", data={"action": "delete", "code": "solo"})

    assert resp.status_code == 200
    assert client.app.state.restarts == [True]
    saved = json.loads(channels_path.read_text(encoding="utf-8"))
    assert saved == []


def test_add_point_requires_same_origin_check(client, tmp_path):
    """action=add_point cung phai bi chan CSRF giong cac action khac (delete/
    add) - Origin header co nhung khac host -> 403, khong ghi file, khong
    trigger restart."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([_single_point_modbus_channel()]), encoding="utf-8")

    resp = client.post("/setup/channels", data=_add_point_form(),
                        headers={"origin": "http://evil.example"})

    assert resp.status_code == 403
    assert client.app.state.restarts == []
    saved = json.loads(channels_path.read_text(encoding="utf-8"))
    assert saved == [_single_point_modbus_channel()]   # khong bi doi


def test_channels_list_page_shows_one_row_per_point(client, tmp_path):
    """GET /setup/channels voi 1 nguon multi-point -> bang phai co 1 dong
    RIENG cho MOI point (dung code point, khong phai code nguon)."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([_multipoint_modbus_channel()]), encoding="utf-8")

    resp = client.get("/setup/channels")

    assert resp.status_code == 200
    assert "<td>a</td><td>modbus</td>" in resp.text
    assert "<td>b</td><td>modbus</td>" in resp.text
    # Code cua CHINH nguon ("src1") khong duoc dung lam 1 dong rieng - chi
    # cac point ben trong moi xuat hien.
    assert "<td>src1</td>" not in resp.text


# ----------------------------------------------------------------------
# 8) Regression cho 2 finding python-reviewer 2026-09-25 (vong 2, sau khi
#    da co Huong B) - _all_codes() Major #2 va _validate_point() overlap
#    Minor - xem docstring _all_codes()/_validate_point() trong settings_api.py.

def test_all_codes_includes_top_level_code_even_when_different_from_all_nested_points():
    """Regression Major #2: TRUOC DAY _all_codes() dung if/else (CHI add
    top-level code KHI KHONG co "points") - bo sot truong hop channels.json
    sua tay co top-level "code" KHAC voi MOI code trong "points". Top-level
    code do CUNG la `value` cua dropdown "Add point" source - thieu no o
    day se cho phep 1 kenh moi dung trung code, va khi trung se lam
    dropdown tro NHAM sang nguon vat ly khac."""
    channels = [{
        "code": "th_sensor", "mode": "modbus", "conn_type": "tcp",
        "host": "10.0.0.9", "tcp_port": 502,
        "points": [{"code": "temp", "reg_offset": 0, "data_type": "u16"},
                   {"code": "humid", "reg_offset": 1, "data_type": "u16"}],
    }]

    codes = settings_api._all_codes(channels)

    assert codes == {"th_sensor", "temp", "humid"}


def test_all_codes_single_point_channel_returns_just_its_own_code():
    """Khong co "points" - hanh vi don gian, chi 1 code cap cao nhat."""
    channels = [{"code": "solo", "mode": "sim"}]

    assert settings_api._all_codes(channels) == {"solo"}


def test_validate_point_rejects_overlapping_reg_offset_with_existing_point():
    """Regression Minor: point moi TRUNG dung reg_offset (0) voi point "a"
    da co trong nguon -> phai bi chan, khong duoc am tham chong lan vung
    thanh ghi (se decode sai/lan lon 2 gia tri tu CUNG 1 thanh ghi vat ly)."""
    base = {
        "code": "src1", "mode": "modbus",
        "points": [
            {"code": "a", "reg_offset": 0, "data_type": "u16"},
            {"code": "b", "reg_offset": 1, "data_type": "f32"},   # chiem reg 1,2
        ],
    }
    values = {"point_code": "c", "point_reg_offset": "0", "point_data_type": "u16",
              "point_scale": "1", "point_offset": "0"}

    errors = settings_api._validate_point(values, base, existing_codes=set())

    assert "point_reg_offset" in errors
    assert "Overlaps" in errors["point_reg_offset"][0]


def test_validate_point_rejects_partial_overlap_with_wider_existing_point():
    """Point rong (f32 chiem 2 thanh ghi: 0,1) - point moi dat o reg_offset=1
    (chi TRUNG 1 phan, khong trung offset dau) van phai bi chan - overlap
    kieu [start,end) giao nhau, khong chi so sanh offset bang nhau."""
    base = {
        "code": "src1", "mode": "modbus",
        "points": [{"code": "a", "reg_offset": 0, "data_type": "f32"}],
    }
    values = {"point_code": "b", "point_reg_offset": "1", "point_data_type": "u16",
              "point_scale": "1", "point_offset": "0"}

    errors = settings_api._validate_point(values, base, existing_codes=set())

    assert "point_reg_offset" in errors


def test_validate_point_accepts_non_overlapping_reg_offset():
    """Point moi dat NGAY SAU point rong nhat (khong giao nhau) -> khong loi."""
    base = {
        "code": "src1", "mode": "modbus",
        "points": [{"code": "a", "reg_offset": 0, "data_type": "u16"}],
    }
    values = {"point_code": "b", "point_reg_offset": "1", "point_data_type": "u16",
              "point_scale": "1", "point_offset": "0"}

    errors = settings_api._validate_point(values, base, existing_codes=set())

    assert "point_reg_offset" not in errors


def test_validate_point_rejects_overlap_when_base_still_single_point_no_points_key_yet():
    """Nguon con la don-diem (CHUA co "points") - overlap check phai tu suy
    ra 1 "point ao" tu chinh du lieu cua base (code/reg_offset=0/data_type)
    de so sanh, khong duoc bo qua overlap chi vi chua co key "points"."""
    base = {"code": "vfd1", "mode": "modbus", "data_type": "f32"}  # chiem reg 0,1
    values = {"point_code": "vfd1_b", "point_reg_offset": "0", "point_data_type": "u16",
              "point_scale": "1", "point_offset": "0"}

    errors = settings_api._validate_point(values, base, existing_codes=set())

    assert "point_reg_offset" in errors
