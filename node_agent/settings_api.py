# -*- coding: utf-8 -*-
"""Trang web cau hinh local cho CHINH node nay (thay the sua .env/channels.json
bang tay qua SSH):
    GET  /setup            form sua .env (NODE_EDGE_URL/SERIAL/NAME/KIND + interval)
    GET  /setup/channels    bang liet ke + form them kenh (sim/serial)
    GET  /setup/channels/scan_ports    liet ke /dev/ttyUSB*|ttyACM*|ttyAMA*
                                        dang co tren thiet bi - JS goi qua nut
                                        "Scan" trong form Add channel
    POST /setup, /setup/channels    ghi file ROI tu thoat sach (sys.exit) de
                                     Docker `restart: always` khoi dong lai
                                     voi config moi - node_agent KHONG ho tro
                                     hot-reload readers giua chung (readers
                                     duoc tao 1 lan luc _build_readers()).

Threat-model + pattern bao mat (CSRF same-origin check, NODE_SETUP_TOKEN
optional HTTP Basic Auth, secrets.compare_digest tranh timing attack) copy
tu ../edge_collector/edge_collector/settings_api.py - sibling project da
giai quyet dung bai toan nay (rui ro CSRF/token-leak qua tunnel), khong tu
nghi lai tu dau. Don gian hoa nhieu so voi ban goc vi node_agent dung chien
luoc "sua xong thi tu restart" thay vi hot-reload singleton settings.
"""
import base64
import glob
import html
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlsplit

from dotenv.main import dotenv_values
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .config import DOTENV_PATH, settings
from .readers.modbus import _reg_width

router = APIRouter()

_ENV_PATH = DOTENV_PATH
_READER_MODES = ("sim", "serial", "modbus", "mqtt", "gpio")

_FIELDS = [
    {"key": "NODE_EDGE_URL", "label": "Edge URL", "default": "http://127.0.0.1:8000",
     "hint": "Address of edge_collector (not Odoo Main)"},
    {"key": "NODE_SERIAL", "label": "Node serial", "default": "NODE-01",
     "hint": "Must match pcm.device.serial in Odoo, or let Odoo auto-create it on first contact"},
    {"key": "NODE_NAME", "label": "Display name", "default": "",
     "hint": "Name Odoo assigns to pcm.device on first self-registration"},
    {"key": "NODE_KIND", "label": "Node kind", "default": "other",
     "hint": "pi | esp32 | pc | other"},
    {"key": "NODE_HELLO_INTERVAL_S", "label": "Hello interval (s)", "default": "60", "hint": ""},
    {"key": "NODE_HEARTBEAT_INTERVAL_S", "label": "Heartbeat interval (s)", "default": "30", "hint": ""},
    {"key": "NODE_SUBMIT_INTERVAL_S", "label": "Submit interval (s)", "default": "2", "hint": ""},
    {"key": "NODE_COMMAND_POLL_INTERVAL_S", "label": "Command poll interval (s)", "default": "2", "hint": ""},
    {"key": "NODE_SETUP_TOKEN", "label": "Setup access token", "default": "",
     "hint": "Leave blank = no gating (LAN-only). Set a value to require HTTP "
             "Basic Auth (any username, password = this token) for all of /setup.",
     "input_type": "password"},
]
_INT_FIELDS = {"NODE_HELLO_INTERVAL_S", "NODE_HEARTBEAT_INTERVAL_S"}
_FLOAT_FIELDS = {"NODE_SUBMIT_INTERVAL_S", "NODE_COMMAND_POLL_INTERVAL_S"}

_CSS = """
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px}
.sub{color:#94a3b8;font-size:13px;margin-bottom:20px}
.card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:20px;margin-bottom:16px}
.field{margin-bottom:14px}
label{display:block;font-size:13px;font-weight:600;margin-bottom:4px}
input,select{width:100%;box-sizing:border-box;background:#0f172a;color:#e2e8f0;
  border:1px solid #334155;border-radius:6px;padding:8px 10px;font-size:14px}
.hint{color:#64748b;font-size:12px;margin-top:4px}
.err{color:#f87171;font-size:12px;margin-top:4px}
button{background:#16a34a;color:#fff;border:none;border-radius:6px;padding:10px 18px;
  font-size:14px;cursor:pointer}
button.danger{background:#dc2626}
.banner-ok{background:#052e16;border:1px solid #16a34a;color:#86efac;border-radius:6px;
  padding:10px 14px;margin-bottom:16px;font-size:13px}
.banner-err{background:#450a0a;border:1px solid #dc2626;color:#fca5a5;border-radius:6px;
  padding:10px 14px;margin-bottom:16px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:16px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #334155}
nav{display:flex;gap:8px;margin-bottom:16px;border-bottom:1px solid #334155}
nav a{color:#94a3b8;font-size:14px;font-weight:600;text-decoration:none;
  padding:10px 18px;border-radius:6px 6px 0 0;border:1px solid transparent}
nav a.active{color:#e2e8f0;background:#1e293b;border-color:#334155;border-bottom-color:#1e293b;
  margin-bottom:-1px}
nav a:not(.active):hover{color:#cbd5e1;background:#1e293b80}
fieldset{border:1px solid #334155;border-radius:6px;margin-bottom:14px}
legend{padding:0 6px;font-size:13px;color:#94a3b8}
"""


def _nav(active: str) -> str:
    def tab(href, label, key):
        cls = ' class="active"' if key == active else ""
        return '<a%s href="%s">%s</a>' % (cls, href, label)
    return "<nav>%s%s</nav>" % (
        tab("/setup", "Node config (.env)", "setup"),
        tab("/setup/channels", "Channels", "channels"))


def _check_setup_auth(request: Request) -> "Response | None":
    """Gate HTTP Basic Auth khi NODE_SETUP_TOKEN duoc dat - copy nguyen ly
    tu edge_collector (secrets.compare_digest tranh timing attack, ma hoa
    UTF-8 truoc khi so de tranh TypeError voi ky tu non-ASCII)."""
    token = settings.setup_token
    if not token:
        return None
    auth = request.headers.get("authorization", "")
    supplied = ""
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
            _, _, supplied = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            supplied = ""
    if supplied and secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        return None
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="node setup"'})


def _is_same_origin(request: Request) -> bool:
    """Chan CSRF don gian - form POST tu trang khac van co the doi .env cua
    nan nhan neu khong kiem tra nay. Copy tu edge_collector (best-effort,
    khong phai auth that, dung threat-model LAN)."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (parsed.scheme, parsed.netloc) == (request.url.scheme, request.url.netloc)


def _current_values() -> dict:
    file_values = dotenv_values(_ENV_PATH) if _ENV_PATH.exists() else {}
    return {f["key"]: file_values.get(f["key"], f["default"]) for f in _FIELDS}


def _format_env_line(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '%s="%s"\n' % (key, escaped)


def _write_env_file(path: Path, values: dict) -> None:
    """Ghi truc tiep vao file dang co (truncate+write, khong tempfile+rename)
    - giu nguyen dong/comment khac, chi thay dong co key trung. Copy chien
    luoc tu edge_collector (tranh 'Device or resource busy' khi .env la
    bind-mount 1 file rieng trong Docker)."""
    remaining = dict(values)
    replaced_keys = set()
    out_lines = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            stripped = line.strip()
            candidate = None
            if stripped and not stripped.startswith("#") and "=" in stripped:
                candidate = stripped.split("=", 1)[0].strip()
            if candidate in replaced_keys:
                continue
            if candidate is not None and candidate in remaining:
                out_lines.append(_format_env_line(candidate, remaining.pop(candidate)))
                replaced_keys.add(candidate)
            else:
                out_lines.append(line if line.endswith("\n") else line + "\n")
    for key, value in remaining.items():
        out_lines.append(_format_env_line(key, value))
    path.write_text("".join(out_lines), encoding="utf-8")


def _validate(values: dict) -> Dict[str, List[str]]:
    errors: Dict[str, List[str]] = {}

    def add(key, msg):
        errors.setdefault(key, []).append(msg)

    for key, value in values.items():
        if "\n" in value or "\r" in value:
            add(key, "Must not contain a newline")
    if not values.get("NODE_EDGE_URL", "").startswith(("http://", "https://")):
        add("NODE_EDGE_URL", "Must start with http:// or https://")
    if not values.get("NODE_SERIAL", "").strip():
        add("NODE_SERIAL", "Must not be blank")
    for key in _INT_FIELDS:
        try:
            if int(values.get(key, "")) <= 0:
                add(key, "Must be a positive integer")
        except ValueError:
            add(key, "Must be an integer")
    for key in _FLOAT_FIELDS:
        try:
            fval = float(values.get(key, ""))
            if not math.isfinite(fval) or fval <= 0:
                add(key, "Must be a positive number")
        except ValueError:
            add(key, "Must be a number")
    return errors


def _render_field(f: dict, values: dict, errors: Dict[str, List[str]]) -> str:
    key = f["key"]
    val = html.escape(str(values.get(key, "")))
    input_type = f.get("input_type", "text")
    err_html = "".join('<p class="err">%s</p>' % html.escape(e) for e in errors.get(key, []))
    return (
        '<div class="field">'
        '<label for="%s">%s</label>'
        '<input type="%s" id="%s" name="%s" value="%s">'
        '%s'
        '<p class="hint">%s</p>'
        '</div>'
    ) % (key, html.escape(f["label"]), input_type, key, key, val, err_html, html.escape(f["hint"]))


def _render_setup(values: dict, errors=None, saved=False) -> HTMLResponse:
    errors = errors or {}
    banner = ""
    if saved:
        banner = ('<div class="banner-ok">Saved. Node is restarting '
                   'de ap dung config moi (restart: always) - vai giay se ket noi lai.</div>')
    if "_form" in errors:
        banner = "".join('<div class="banner-err">%s</div>' % html.escape(e) for e in errors["_form"])
    fields_html = "".join(_render_field(f, values, errors) for f in _FIELDS)
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Node Agent Setup</title><style>%s</style></head><body>"
        "<div class='wrap'>%s<h1>Node Agent</h1>"
        "<p class='sub'>Node serial: %s</p>"
        "<div class='card'>%s"
        "<form method='post' action='/setup'>%s"
        "<button type='submit'>Save</button></form></div></div>"
        "</body></html>"
    ) % (_CSS, _nav("setup"), html.escape(settings.serial), banner, fields_html)
    return HTMLResponse(body)


@router.get("/", include_in_schema=False)
async def root_redirect():
    """Tien loi truy cap - "/" khong co noi dung rieng, chuyen thang sang
    /setup. Redirect thuan tuy, khong lo du lieu gi (auth gate van ap dung
    o chinh /setup khi trinh duyet theo redirect toi)."""
    return RedirectResponse(url="/setup")


@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    denied = _check_setup_auth(request)
    if denied:
        return denied
    return _render_setup(_current_values())


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(request: Request):
    denied = _check_setup_auth(request)
    if denied:
        return denied
    if not _is_same_origin(request):
        return HTMLResponse(
            _render_setup(_current_values(),
                          errors={"_form": ["Rejected: request did not originate from the /setup "
                                             "page (possible CSRF) - reopen /setup and save from there"]}).body,
            status_code=403)
    form = await request.form()
    values = {f["key"]: str(form.get(f["key"], "")).strip() for f in _FIELDS}
    errors = _validate(values)
    if errors:
        return HTMLResponse(_render_setup(values, errors=errors).body, status_code=400)
    if values == _current_values():
        # Khong co gi thay doi - khong trigger restart (xem _schedule_restart,
        # DoS-guard 2026-09-25).
        return _render_setup(values, saved=True)
    try:
        _write_env_file(_ENV_PATH, values)
    except OSError as exc:
        return HTMLResponse(
            _render_setup(_current_values(),
                          errors={"_form": ["Could not write .env: %s - check file write permission "
                                             "(container runs as uid 1000, see README)" % exc]}).body,
            status_code=500)
    _schedule_restart()
    return _render_setup(values, saved=True)


# ----------------------------------------------------------------------
# Channels editor

def _load_channels() -> list:
    return settings.load_channels()


def _all_codes(channels: list) -> set:
    """Every channel code actually in use, including codes nested inside a
    Modbus multi-point source's "points" list (Approach B) - a flat
    {c.get("code") for c in channels} would miss those and let a duplicate
    slip in. Always includes the entry's own top-level `code` too, even for
    a multi-point source: hand-edited channels.json can leave it different
    from every points[].code, and that top-level code is also the `value`
    used by the "Add point" source dropdown - missing it here would let a
    new channel silently collide with it, and a collision there would make
    the dropdown resolve to the WRONG physical source (python-reviewer
    2026-09-25 Major finding)."""
    codes = set()
    for c in channels:
        codes.add(c.get("code"))
        if c.get("mode") == "modbus" and c.get("points"):
            codes.update(p.get("code") for p in c["points"])
    return codes


def _write_channels(channels: list) -> None:
    Path(settings.channels_file).write_text(
        json.dumps(channels, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _unescape_terminator(s: str) -> str:
    return s.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")


def _port_field_html(errors: Dict[str, List[str]]) -> str:
    err_html = "".join('<p class="err">%s</p>' % html.escape(e) for e in errors.get("port", []))
    return (
        '<div class="field"><label>Port</label>'
        '<div style="display:flex;gap:8px">'
        '<input type="text" name="port" id="port-input" value="/dev/ttyUSB0" style="flex:1">'
        '<button type="button" onclick="scanPorts()">Scan</button></div>'
        '<div id="port-suggestions" class="hint"></div>%s</div>'
    ) % err_html


def _validate_channel(values: dict, existing_codes: set) -> Dict[str, List[str]]:
    errors: Dict[str, List[str]] = {}

    def add(key, msg):
        errors.setdefault(key, []).append(msg)

    code = values.get("code", "").strip()
    if not code:
        add("code", "Must not be blank")
    elif code in existing_codes:
        add("code", "Channel code '%s' already exists" % code)
    if values.get("mode") not in _READER_MODES:
        add("mode", "Must be sim, serial, modbus, mqtt or gpio")
    if values.get("mode") == "sim":
        for key in ("poll_ms",):
            try:
                if int(values.get(key, "")) <= 0:
                    add(key, "Must be a positive integer")
            except ValueError:
                add(key, "Must be an integer")
        for key in ("center", "spread"):
            try:
                float(values.get(key, ""))
            except ValueError:
                add(key, "Must be a number")
    elif values.get("mode") == "serial":
        if not values.get("port", "").strip():
            add("port", "Must not be blank")
        try:
            if int(values.get("baud", "")) <= 0:
                add("baud", "Must be a positive integer")
        except ValueError:
            add("baud", "Must be an integer")
        if values.get("data_bits") not in ("7", "8"):
            add("data_bits", "Must be 7 or 8")
        if values.get("parity") not in ("none", "even", "odd"):
            add("parity", "Must be none/even/odd")
        pattern = values.get("pattern", "").strip()
        if not pattern:
            add("pattern", "Must not be blank")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                # BAT BUOC validate truoc khi ghi - _build_readers() (agent.py)
                # goi re.compile() KHI KHOI DONG, khong bọc try/except; pattern
                # loi se lam CA process crash ngay luc start, va vi Docker
                # `restart: always` cu khoi dong lai voi DUNG config loi do ->
                # crash-loop vinh vien, phai SSH sua tay channels.json moi
                # thoat duoc - xem python-reviewer 2026-09-25.
                add("pattern", "Invalid regex: %s" % exc)
        # value_group/stop_bits/stable_group deu duoc int() truc tiep o
        # _channel_to_json - validate som o day de tra loi form ro rang thay
        # vi HTTP 500 cau - xem python-reviewer 2026-09-25.
        for key in ("value_group", "stop_bits"):
            raw = values.get(key, "").strip()
            if raw:
                try:
                    int(raw)
                except ValueError:
                    add(key, "Must be an integer")
        stable_group_raw = values.get("stable_group", "").strip()
        if stable_group_raw:
            try:
                int(stable_group_raw)
            except ValueError:
                add("stable_group", "Must be an integer")
    elif values.get("mode") == "modbus":
        if values.get("conn_type") not in ("tcp", "rtu"):
            add("conn_type", "Must be tcp or rtu")
        elif values["conn_type"] == "tcp":
            if not values.get("host", "").strip():
                add("host", "Must not be blank")
            try:
                if int(values.get("tcp_port", "")) <= 0:
                    add("tcp_port", "Must be a positive integer")
            except ValueError:
                add("tcp_port", "Must be an integer")
        else:
            if not values.get("modbus_port", "").strip():
                add("modbus_port", "Must not be blank")
            try:
                if int(values.get("modbus_baud", "")) <= 0:
                    add("modbus_baud", "Must be a positive integer")
            except ValueError:
                add("modbus_baud", "Must be an integer")
        try:
            if int(values.get("unit_id", "")) <= 0:
                add("unit_id", "Must be a positive integer")
        except ValueError:
            add("unit_id", "Must be an integer")
        if values.get("register_type") not in ("holding", "input"):
            add("register_type", "Must be holding or input")
        try:
            if int(values.get("address", "")) < 0:
                add("address", "Must be a non-negative integer")
        except ValueError:
            add("address", "Must be an integer")
        if values.get("data_type") not in ("u16", "i16", "u32", "i32", "f32"):
            add("data_type", "Must be one of u16/i16/u32/i32/f32")
        for key in ("scale", "offset"):
            try:
                float(values.get(key, ""))
            except ValueError:
                add(key, "Must be a number")
        try:
            if int(values.get("modbus_poll_ms", "")) <= 0:
                add("modbus_poll_ms", "Must be a positive integer")
        except ValueError:
            add("modbus_poll_ms", "Must be an integer")
    elif values.get("mode") == "mqtt":
        if not values.get("mqtt_host", "").strip():
            add("mqtt_host", "Must not be blank")
        try:
            if int(values.get("mqtt_port", "")) <= 0:
                add("mqtt_port", "Must be a positive integer")
        except ValueError:
            add("mqtt_port", "Must be an integer")
        if not values.get("topic", "").strip():
            add("topic", "Must not be blank")
    elif values.get("mode") == "gpio":
        try:
            if int(values.get("pin", "")) < 0:
                add("pin", "Must be a non-negative integer")
        except ValueError:
            add("pin", "Must be an integer")
        if values.get("pull_up") not in ("true", "false"):
            add("pull_up", "Must be true or false")
        if values.get("invert") not in ("true", "false"):
            add("invert", "Must be true or false")
        try:
            if int(values.get("bounce_ms", "")) < 0:
                add("bounce_ms", "Must be a non-negative integer")
        except ValueError:
            add("bounce_ms", "Must be an integer")
        try:
            if int(values.get("gpio_poll_ms", "")) <= 0:
                add("gpio_poll_ms", "Must be a positive integer")
        except ValueError:
            add("gpio_poll_ms", "Must be an integer")
    return errors


def _validate_point(values: dict, base, existing_codes: set) -> Dict[str, List[str]]:
    """Validate a new point being added to an existing Modbus source
    (Approach B - see readers/modbus.py). `base` is the matched source
    entry from channels.json, or None if the selected source code wasn't
    found."""
    errors: Dict[str, List[str]] = {}

    def add(key, msg):
        errors.setdefault(key, []).append(msg)

    if base is None:
        add("point_source_code", "Source not found")
    code = values.get("point_code", "").strip()
    if not code:
        add("point_code", "Must not be blank")
    elif code in existing_codes:
        add("point_code", "Channel code '%s' already exists" % code)
    raw_offset = values.get("point_reg_offset", "").strip()
    offset = None
    try:
        offset = int(raw_offset)
        if offset < 0:
            add("point_reg_offset", "Must be a non-negative integer")
            offset = None
    except ValueError:
        add("point_reg_offset", "Must be an integer")
    dtype = values.get("point_data_type")
    if dtype not in ("u16", "i16", "u32", "i32", "f32"):
        add("point_data_type", "Must be one of u16/i16/u32/i32/f32")
        dtype = None
    for key in ("point_scale", "point_offset"):
        raw = values.get(key, "").strip()
        if raw:
            try:
                float(raw)
            except ValueError:
                add(key, "Must be a number")
    # A silently overlapping register range would decode two points from
    # the SAME physical registers, corrupting the measurement pipeline
    # without any error or warning anywhere - check it whenever we have
    # enough valid info to compute the new point's [offset, offset+width)
    # range (python-reviewer 2026-09-25 Minor finding).
    if base is not None and offset is not None and dtype is not None:
        new_width = _reg_width(dtype)
        existing_points = base.get("points") or [
            {"code": base.get("code"), "reg_offset": 0, "data_type": base.get("data_type", "u16")}]
        for p in existing_points:
            p_offset = p.get("reg_offset", 0)
            p_width = _reg_width(p.get("data_type", "u16"))
            if offset < p_offset + p_width and p_offset < offset + new_width:
                add("point_reg_offset", "Overlaps with existing point '%s' (offset %s, width %s)" % (
                    p.get("code"), p_offset, p_width))
                break
    return errors


def _channel_to_json(values: dict) -> dict:
    if values["mode"] == "sim":
        return {
            "code": values["code"], "mode": "sim",
            "poll_ms": int(values["poll_ms"]),
            "center": float(values["center"]), "spread": float(values["spread"]),
        }
    if values["mode"] == "modbus":
        ch = {
            "code": values["code"], "mode": "modbus", "conn_type": values["conn_type"],
            "unit_id": int(values["unit_id"]), "register_type": values["register_type"],
            "address": int(values["address"]), "data_type": values["data_type"],
            "scale": float(values.get("scale") or 1), "offset": float(values.get("offset") or 0),
            "poll_ms": int(values["modbus_poll_ms"]),
        }
        if values["conn_type"] == "tcp":
            ch["host"] = values["host"]
            ch["tcp_port"] = int(values["tcp_port"])
        else:
            ch["port"] = values["modbus_port"]
            ch["baud"] = int(values["modbus_baud"])
        return ch
    if values["mode"] == "mqtt":
        ch = {
            "code": values["code"], "mode": "mqtt",
            "host": values["mqtt_host"], "port": int(values["mqtt_port"]),
            "topic": values["topic"],
        }
        if values.get("username"):
            ch["username"] = values["username"]
        if values.get("password"):
            ch["password"] = values["password"]
        if values.get("json_key"):
            ch["json_key"] = values["json_key"]
        if values.get("cmd_topic"):
            ch["cmd_topic"] = values["cmd_topic"]
        return ch
    if values["mode"] == "gpio":
        return {
            "code": values["code"], "mode": "gpio", "pin": int(values["pin"]),
            "pull_up": values["pull_up"] == "true", "invert": values["invert"] == "true",
            "bounce_ms": int(values.get("bounce_ms") or 0),
            "poll_ms": int(values["gpio_poll_ms"]),
        }
    ch = {
        "code": values["code"], "mode": "serial", "port": values["port"],
        "baud": int(values["baud"]), "data_bits": int(values["data_bits"]),
        "parity": values["parity"], "stop_bits": int(values.get("stop_bits") or 1),
        "terminator": _unescape_terminator(values.get("terminator") or "\\r\\n"),
        "pattern": values["pattern"],
        "value_group": int(values.get("value_group") or 1),
    }
    if values.get("stable_group"):
        ch["stable_group"] = int(values["stable_group"])
    if values.get("stable_ok"):
        ch["stable_ok"] = values["stable_ok"]
    if values.get("cmd_zero"):
        ch["cmd_zero"] = _unescape_terminator(values["cmd_zero"])
    if values.get("cmd_tare"):
        ch["cmd_tare"] = _unescape_terminator(values["cmd_tare"])
    return ch


def _render_channels(errors=None, saved=False, deleted=False) -> HTMLResponse:
    errors = errors or {}
    channels = _load_channels()
    banner = ""
    if saved:
        banner = '<div class="banner-ok">Channel added. Node is restarting.</div>'
    if deleted:
        banner = '<div class="banner-ok">Channel deleted. Node is restarting.</div>'
    if "_form" in errors:
        banner = "".join('<div class="banner-err">%s</div>' % html.escape(e) for e in errors["_form"])

    def _modbus_endpoint(ch: dict) -> str:
        return (ch.get("host", "") + ":" + str(ch.get("tcp_port", "")) if ch.get("conn_type") == "tcp"
                else ch.get("port", ""))

    def _channel_summary(ch: dict) -> str:
        mode = ch.get("mode")
        if mode == "serial":
            return ch.get("port", "")
        if mode == "modbus":
            return "%s unit=%s reg=%s@%s" % (
                _modbus_endpoint(ch), ch.get("unit_id"), ch.get("register_type"), ch.get("address"))
        if mode == "mqtt":
            return "%s:%s topic=%s" % (ch.get("host", ""), ch.get("port", ""), ch.get("topic", ""))
        if mode == "gpio":
            return "pin=%s pull_up=%s invert=%s" % (
                ch.get("pin"), ch.get("pull_up"), ch.get("invert"))
        return "center=%s" % ch.get("center", "")

    def _channel_row(code: str, mode: str, detail: str) -> str:
        return (
            "<tr><td>%s</td><td>%s</td><td>%s</td>"
            "<td><form method='post' action='/setup/channels' style='margin:0'>"
            "<input type='hidden' name='action' value='delete'>"
            "<input type='hidden' name='code' value='%s'>"
            "<button type='submit' class='danger'>Delete</button></form></td></tr>"
        ) % (html.escape(code), html.escape(mode), html.escape(detail), html.escape(code))

    rows = ""
    for ch in channels:
        if ch.get("mode") == "modbus" and ch.get("points"):
            # Approach B: a source may represent MULTIPLE real data channels
            # (points) - list one row per point (each with its own delete
            # button), not one row for the whole source, so Nam can remove
            # a single point without losing the others sharing the same
            # physical connection.
            endpoint = _modbus_endpoint(ch)
            for p in ch["points"]:
                detail = "%s unit=%s reg=%s@%s+%s" % (
                    endpoint, ch.get("unit_id"), ch.get("register_type"),
                    ch.get("address"), p.get("reg_offset", 0))
                rows += _channel_row(p.get("code", ""), "modbus", detail)
        else:
            rows += _channel_row(ch.get("code", ""), ch.get("mode", ""), _channel_summary(ch))
    table = ("<table><tr><th>Code</th><th>Mode</th><th>Detail</th><th></th></tr>%s</table>"
              % rows) if channels else "<p class='hint'>No channels yet.</p>"

    def field(name, label, value="", err_key=None, itype="text"):
        err_html = "".join('<p class="err">%s</p>' % html.escape(e)
                            for e in errors.get(err_key or name, []))
        return ('<div class="field"><label>%s</label>'
                '<input type="%s" name="%s" value="%s">%s</div>') % (
            html.escape(label), itype, name, html.escape(str(value)), err_html)

    add_form = (
        "<form method='post' action='/setup/channels'>"
        "<input type='hidden' name='action' value='add'>"
        + field("code", "Code")
        + '<div class="field"><label>Mode</label>'
          '<select name="mode" id="mode-select" onchange="'
          "document.getElementById('sim-fields').style.display="
          "this.value=='sim'?'block':'none';"
          "document.getElementById('serial-fields').style.display="
          "this.value=='serial'?'block':'none';"
          "document.getElementById('modbus-fields').style.display="
          "this.value=='modbus'?'block':'none';"
          "document.getElementById('mqtt-fields').style.display="
          "this.value=='mqtt'?'block':'none';"
          "document.getElementById('gpio-fields').style.display="
          "this.value=='gpio'?'block':'none';\">"
          '<option value="sim">sim</option><option value="serial">serial</option>'
          '<option value="modbus">modbus</option><option value="mqtt">mqtt</option>'
          '<option value="gpio">gpio</option></select></div>'
        + "<fieldset id='sim-fields'><legend>Sim</legend>"
        + field("poll_ms", "Poll (ms)", "500")
        + field("center", "Center", "0")
        + field("spread", "Spread", "0.1")
        + "</fieldset>"
        + "<fieldset id='serial-fields' style='display:none'><legend>Serial</legend>"
        + _port_field_html(errors)
        + field("baud", "Baud", "9600")
        + field("data_bits", "Data bits (7/8)", "8")
        + field("parity", "Parity (none/even/odd)", "none")
        + field("stop_bits", "Stop bits", "1")
        + field("terminator", "Terminator", "\\r\\n")
        + field("pattern", "Pattern (regex)", "")
        + field("value_group", "Value group", "1")
        + field("stable_group", "Stable group", "")
        + field("stable_ok", "Stable ok", "")
        + field("cmd_zero", "Cmd zero", "")
        + field("cmd_tare", "Cmd tare", "")
        + "</fieldset>"
        + "<fieldset id='modbus-fields' style='display:none'><legend>Modbus</legend>"
        + ('<div class="field"><label>Connection type</label>'
           '<select name="conn_type" id="modbus-conn-select" onchange="'
           "document.getElementById('modbus-tcp-fields').style.display="
           "this.value=='tcp'?'block':'none';"
           "document.getElementById('modbus-rtu-fields').style.display="
           "this.value=='rtu'?'block':'none';\">"
           '<option value="tcp">tcp</option><option value="rtu">rtu</option></select>%s</div>'
           % "".join('<p class="err">%s</p>' % html.escape(e) for e in errors.get("conn_type", [])))
        + "<div id='modbus-tcp-fields'>"
        + field("host", "Host", "127.0.0.1")
        + field("tcp_port", "TCP port", "502")
        + "</div>"
        + "<div id='modbus-rtu-fields' style='display:none'>"
        + field("modbus_port", "Serial port", "/dev/ttyUSB0")
        + field("modbus_baud", "Baud", "9600")
        + "</div>"
        + field("unit_id", "Unit id (slave address)", "1")
        + field("register_type", "Register type (holding/input)", "holding")
        + field("address", "Register address", "0")
        + field("data_type", "Data type (u16/i16/u32/i32/f32)", "u16")
        + field("scale", "Scale", "1")
        + field("offset", "Offset", "0")
        + field("modbus_poll_ms", "Poll (ms)", "1000")
        + "</fieldset>"
        + "<fieldset id='mqtt-fields' style='display:none'><legend>MQTT</legend>"
        + field("mqtt_host", "Broker host", "127.0.0.1")
        + field("mqtt_port", "Broker port", "1883")
        + field("username", "Username (optional)", "")
        + field("password", "Password (optional)", "", itype="password")
        + field("topic", "Subscribe topic", "")
        + field("json_key", "JSON key (blank = raw numeric payload)", "")
        + field("cmd_topic", "Publish topic for commands (blank = <topic>/cmd)", "")
        + "</fieldset>"
        + "<fieldset id='gpio-fields' style='display:none'><legend>GPIO</legend>"
        + field("pin", "GPIO pin (BCM numbering)", "4")
        + field("pull_up", "Pull up (true/false)", "false")
        + field("invert", "Invert reading (true/false)", "false")
        + field("bounce_ms", "Debounce (ms, 0 = off)", "0")
        + field("gpio_poll_ms", "Poll (ms)", "200")
        + "</fieldset>"
        + "<button type='submit'>Add channel</button></form>"
    )

    # Approach B: attach an additional point to an EXISTING Modbus source
    # (same physical connection) instead of creating an independent one -
    # needed for devices that only answer a single combined read (see
    # readers/modbus.py). Source options are existing Modbus channel codes
    # (works for both a plain single-point channel, which gets converted to
    # a multi-point source on first use, and an already multi-point one).
    modbus_source_codes = [c["code"] for c in channels if c.get("mode") == "modbus" and c.get("code")]
    source_options = "".join(
        '<option value="%s">%s</option>' % (html.escape(c), html.escape(c)) for c in modbus_source_codes
    ) or '<option value="" disabled selected>No Modbus source yet</option>'
    add_point_form = (
        "<form method='post' action='/setup/channels'>"
        "<input type='hidden' name='action' value='add_point'>"
        "<fieldset><legend>Add point to existing Modbus source</legend>"
        + ('<div class="field"><label>Source</label><select name="point_source_code">%s</select>%s</div>'
           % (source_options, "".join('<p class="err">%s</p>' % html.escape(e)
                                       for e in errors.get("point_source_code", []))))
        + field("point_code", "New point code")
        + field("point_reg_offset", "Register offset (from source's address)", "1")
        + field("point_data_type", "Data type (u16/i16/u32/i32/f32)", "u16")
        + field("point_scale", "Scale", "1")
        + field("point_offset", "Offset", "0")
        + "</fieldset>"
        + "<button type='submit'>Add point</button></form>"
    )
    scan_script = (
        "<script>"
        "async function scanPorts(){"
        "const box=document.getElementById('port-suggestions');"
        "box.textContent='Scanning...';"
        "try{"
        "const res=await fetch('/setup/channels/scan_ports');"
        "const data=await res.json();"
        "if(!data.ports||data.ports.length===0){"
        "box.textContent='No serial ports found.';return;}"
        "box.innerHTML='';"
        "data.ports.forEach(function(p){"
        "const b=document.createElement('button');"
        "b.type='button';b.textContent=p;"
        "b.style.marginRight='6px';b.style.marginTop='4px';"
        "b.onclick=function(){document.getElementById('port-input').value=p;};"
        "box.appendChild(b);});"
        "}catch(e){box.textContent='Scan error: '+e;}"
        "}"
        "</script>"
    )
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Node Agent Channels</title><style>%s</style></head><body>"
        "<div class='wrap'>%s<h1>Channels</h1>%s"
        "<div class='card'>%s</div>"
        "<div class='card'>%s</div>"
        "<div class='card'>%s</div></div>%s</body></html>"
    ) % (_CSS, _nav("channels"), banner, table, add_form, add_point_form, scan_script)
    return HTMLResponse(body)


@router.get("/setup/channels", response_class=HTMLResponse)
async def channels_get(request: Request):
    denied = _check_setup_auth(request)
    if denied:
        return denied
    return _render_channels()


def _scan_serial_ports() -> list:
    patterns = ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*")
    ports = set()
    for pat in patterns:
        ports.update(glob.glob(pat))
    return sorted(ports)


@router.get("/setup/channels/scan_ports")
async def scan_ports(request: Request):
    """Liet ke cong serial dang co tren CHINH thiet bi nay (khong phai may
    dang mo trinh duyet) - JS goi qua nut Scan trong form Add channel, tranh
    Nam phai tu go tay `/dev/ttyUSB0`/`/dev/ttyUSB1`/... roi thu sai lien tuc
    moi lan cam cam bien vao cong khac (da gap that: cung 1 loai adapter
    PL2303 co the len ttyUSB0 hay ttyUSB1 tuy thu tu cam/thiet bi khac dang
    chiem)."""
    denied = _check_setup_auth(request)
    if denied:
        return denied
    return {"ports": _scan_serial_ports()}


@router.post("/setup/channels", response_class=HTMLResponse)
async def channels_post(request: Request):
    denied = _check_setup_auth(request)
    if denied:
        return denied
    if not _is_same_origin(request):
        return HTMLResponse(
            _render_channels(errors={"_form": ["Rejected: possible CSRF - reopen /setup/channels"]}).body,
            status_code=403)
    form = await request.form()
    action = form.get("action")
    channels = _load_channels()
    if action == "delete":
        code = form.get("code")
        new_channels = []
        changed = False
        for c in channels:
            if c.get("mode") == "modbus" and c.get("points"):
                # Approach B: `code` may name a single point of a
                # multi-point source rather than the source entry itself -
                # remove just that point, dropping the whole entry only if
                # it was the last point left.
                kept = [p for p in c["points"] if p.get("code") != code]
                if len(kept) == len(c["points"]):
                    new_channels.append(c)
                    continue
                changed = True
                if kept:
                    c = dict(c, points=kept)
                    new_channels.append(c)
            elif c.get("code") == code:
                changed = True
            else:
                new_channels.append(c)
        if not changed:
            # Code khong ton tai - khong co gi thay doi, dung trigger restart
            # (xem _schedule_restart, DoS-guard 2026-09-25).
            return _render_channels(deleted=True)
        try:
            _write_channels(new_channels)
        except OSError as exc:
            return HTMLResponse(
                _render_channels(errors={"_form": ["Could not write channels.json: %s - check file write "
                                                     "permission (remove `:ro` in docker-compose.yml "
                                                     "if mounted read-only)" % exc]}).body,
                status_code=500)
        _schedule_restart()
        return _render_channels(deleted=True)
    if action == "add":
        values = {k: str(form.get(k, "")).strip() for k in
                  ("code", "mode", "poll_ms", "center", "spread", "port", "baud",
                   "data_bits", "parity", "stop_bits", "terminator", "pattern",
                   "value_group", "stable_group", "stable_ok", "cmd_zero", "cmd_tare",
                   "conn_type", "host", "tcp_port", "modbus_port", "modbus_baud",
                   "unit_id", "register_type", "address", "data_type", "scale",
                   "offset", "modbus_poll_ms", "mqtt_host", "mqtt_port", "username",
                   "password", "topic", "json_key", "cmd_topic", "pin", "pull_up",
                   "invert", "bounce_ms", "gpio_poll_ms")}
        errors = _validate_channel(values, _all_codes(channels))
        if errors:
            return HTMLResponse(_render_channels(errors=errors).body, status_code=400)
        channels.append(_channel_to_json(values))
        try:
            _write_channels(channels)
        except OSError as exc:
            return HTMLResponse(
                _render_channels(errors={"_form": ["Could not write channels.json: %s - check file write "
                                                     "permission (remove `:ro` in docker-compose.yml "
                                                     "if mounted read-only)" % exc]}).body,
                status_code=500)
        _schedule_restart()
        return _render_channels(saved=True)
    if action == "add_point":
        values = {k: str(form.get(k, "")).strip() for k in
                  ("point_source_code", "point_code", "point_reg_offset",
                   "point_data_type", "point_scale", "point_offset")}
        base = next((c for c in channels
                     if c.get("mode") == "modbus" and c.get("code") == values["point_source_code"]), None)
        errors = _validate_point(values, base, _all_codes(channels))
        if errors:
            return HTMLResponse(_render_channels(errors=errors).body, status_code=400)
        if not base.get("points"):
            # First point being added to a plain single-point Modbus
            # channel - convert it into a multi-point source, preserving
            # its own code/data_type/scale/offset as point index 0 so its
            # existing data (and any pending command) keeps working.
            base["points"] = [{
                "code": base["code"], "reg_offset": 0,
                "data_type": base.pop("data_type", "u16"),
                "scale": base.pop("scale", 1), "offset": base.pop("offset", 0),
            }]
        base["points"].append({
            "code": values["point_code"],
            "reg_offset": int(values["point_reg_offset"]),
            "data_type": values["point_data_type"],
            "scale": float(values.get("point_scale") or 1),
            "offset": float(values.get("point_offset") or 0),
        })
        try:
            _write_channels(channels)
        except OSError as exc:
            return HTMLResponse(
                _render_channels(errors={"_form": ["Could not write channels.json: %s - check file write "
                                                     "permission (remove `:ro` in docker-compose.yml "
                                                     "if mounted read-only)" % exc]}).body,
                status_code=500)
        _schedule_restart()
        return _render_channels(saved=True)
    return HTMLResponse(_render_channels(errors={"_form": ["Invalid action"]}).body,
                         status_code=400)


_RESTART_COOLDOWN_S = 10.0


def _restart_marker_path() -> Path:
    return Path(settings.state_dir) / "last_setup_restart"


def _schedule_restart() -> bool:
    """Node khong ho tro hot-reload readers giua chung (duoc tao 1 lan luc
    _build_readers()) - cach don gian nhat de config moi co hieu luc la tu
    thoat sach de Docker `restart: always` khoi dong lai tien trinh voi
    .env/channels.json moi. Dat trong os._exit thread rieng + delay ngan de
    HTTP response kip gui ve trinh duyet TRUOC khi tien trinh chet - _exit
    (khong phai sys.exit) vi goi tu thread khac main thread, sys.exit chi
    thoat thread hien tai.

    Cooldown _RESTART_COOLDOWN_S: NODE_SETUP_TOKEN mac dinh rong (khong gate
    gi) + _is_same_origin() cho qua request thieu Origin (vd `curl`) - khong
    co cooldown, spam POST /setup se kill toan bo tien trinh (ca outbox/
    reader/hello loop, khong chi web UI) lien tuc vo thoi han = DoS de dang.
    PHAI persist cooldown xuong FILE (mtime cua marker trong state_dir, mount
    qua named volume nen song sot qua restart), KHONG dung bien RAM - bien
    RAM (`_last_restart_at` truoc day) bi chinh os._exit() xoa sach moi lan
    trigger, nen process MOI sau restart luon thay "chua restart lan nao" va
    cho qua ngay lap tuc - khong chan duoc dung kich ban tan cong that (1
    request/lan container vua len lai) - xem python-reviewer 2026-09-25
    (vong 2, bat duoc round-trip logic sai o vong 1). Tra False (khong
    restart) neu goi lai qua gan lan truoc, KHONG bao gio bao loi cho nguoi
    dung that (Save van thanh cong that su, chi delay ap dung toi da
    _RESTART_COOLDOWN_S)."""
    marker = _restart_marker_path()
    now = time.time()
    try:
        last = marker.stat().st_mtime
    except OSError:
        last = 0.0
    if now - last < _RESTART_COOLDOWN_S:
        return False
    try:
        marker.touch()
    except OSError:
        pass          # best-effort - khong chan restart chi vi ghi marker loi

    def _die():
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return True
