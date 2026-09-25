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
    assert "Regex khong hop le" in errors["pattern"][0]


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
    assert "Da luu" in second.text
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
    assert "Khong ghi duoc channels.json" in resp.text
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
    assert "Khong ghi duoc channels.json" in resp.text
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
