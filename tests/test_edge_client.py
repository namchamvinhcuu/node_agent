# -*- coding: utf-8 -*-
"""Test node_agent/edge_client.py::EdgeClient.hello() (pcm-downlink-command,
2026-10-02): hoc clock_offset_s tu server_time_ms (gio edge - gio node) de
"ts" lenh xuong khong bi tu choi nham "stale" tren node khong NTP; va
invariant CLAUDE.md muc 6: hello() KHONG BAO GIO gui kem X-API-Key."""
import time
from unittest.mock import Mock

import pytest

import node_agent.config as config_module
from node_agent.edge_client import EdgeClient

OLD_KEY = "khoa-cu-dang-giu"  # secret-allow (test fixture)


def _client(monkeypatch, response_json, ok=True):
    monkeypatch.setattr(config_module.settings, "edge_url", "http://edge.test")
    store = Mock()
    store.kv_get.return_value = OLD_KEY
    c = EdgeClient(store)
    resp = Mock(ok=ok)
    resp.json.return_value = response_json
    c.session = Mock()
    c.session.post.return_value = resp
    return c


def test_hello_learns_clock_offset_from_server_time_ms(monkeypatch):
    server_ms = (time.time() + 3600) * 1000      # edge nhanh hon node 1 gio
    c = _client(monkeypatch, {"ok": True, "server_time_ms": server_ms})

    c.hello()

    assert c.clock_offset_s == pytest.approx(3600, abs=1.0)


def test_hello_learns_negative_offset_int_ms(monkeypatch):
    server_ms = int((time.time() - 50) * 1000)
    c = _client(monkeypatch, {"ok": True, "server_time_ms": server_ms})

    c.hello()

    assert c.clock_offset_s == pytest.approx(-50, abs=1.0)


@pytest.mark.parametrize("body", [
    pytest.param({"ok": True}, id="thieu-server_time_ms"),
    pytest.param({"ok": True, "server_time_ms": "123"}, id="chuoi"),
    pytest.param({"ok": True, "server_time_ms": None}, id="null"),
    pytest.param({"ok": True, "server_time_ms": True}, id="bool"),
    pytest.param({"ok": False, "server_time_ms": 9_999_999_999_999}, id="ok-false"),
])
def test_hello_ignores_missing_or_invalid_server_time(monkeypatch, body):
    c = _client(monkeypatch, body)
    c.clock_offset_s = 0.0

    c.hello()

    assert c.clock_offset_s == 0.0


def test_hello_non_object_body_does_not_crash_and_keeps_offset(monkeypatch):
    c = _client(monkeypatch, [1, 2, 3])

    res = c.hello()

    assert res["raw"] == [1, 2, 3]
    assert c.clock_offset_s == 0.0


def test_hello_never_sends_api_key_even_when_one_is_stored(monkeypatch):
    c = _client(monkeypatch, {"ok": True, "server_time_ms": time.time() * 1000})

    c.hello()

    url = c.session.post.call_args.args[0]
    headers = c.session.post.call_args.kwargs["headers"]
    assert url.endswith("/node/v1/hello")
    assert "X-API-Key" not in headers
    assert headers["X-Device-Serial"] == config_module.settings.serial


def test_ack_command_body_with_and_without_request_id(monkeypatch):
    c = _client(monkeypatch, {"ok": True})

    c.ack_command(1, True, "", request_id="R-1")
    c.ack_command(2, False, "x")

    bodies = [call.kwargs["json"] for call in c.session.post.call_args_list]
    assert bodies == [
        {"id": 1, "ok": True, "detail": "", "request_id": "R-1"},
        {"id": 2, "ok": False, "detail": "x"},
    ]
    assert c.session.post.call_args.kwargs["headers"]["X-API-Key"] == OLD_KEY
