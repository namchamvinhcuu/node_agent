# -*- coding: utf-8 -*-
"""Client goi VAO edge - dung hop dong node_api.py (edge_collector). Dong bo
(requests) vi node chi can mot vong lap tuan tu don gian, khong can asyncio."""
import logging

import requests

from .config import settings
from .store import Store

_logger = logging.getLogger("node.edge_client")
_TIMEOUT = 6.0


def _normalize_body(body_out, ok: bool) -> dict:
    # r.json() co the tra ve JSON hop le nhung KHONG phai object (vd list/
    # chuoi/so) - body_out.setdefault(...) se raise AttributeError khong bi
    # bat neu khong bao boc truoc. Cung lop bug da fix o
    # edge_collector/odoo_client.py::_parse() (2026-09-24).
    if not isinstance(body_out, dict):
        body_out = {"raw": body_out}
    body_out.setdefault("ok", ok)
    return body_out


class EdgeClient:
    def __init__(self, store: Store):
        self.store = store
        self.session = requests.Session()

    @property
    def api_key(self):
        return self.store.kv_get("api_key")

    def _headers(self, include_key: bool = True) -> dict:
        h = {"X-Device-Serial": settings.serial, "Content-Type": "application/json"}
        key = self.api_key if include_key else None
        if key:
            h["X-API-Key"] = key
        return h

    def _post(self, path: str, body: dict, include_key: bool = True) -> dict:
        try:
            r = self.session.post(settings.edge_url + path, json=body,
                                  headers=self._headers(include_key), timeout=_TIMEOUT)
            body_out = r.json()
        except (requests.RequestException, ValueError) as exc:
            _logger.info("edge unreachable POST %s: %s", path, exc)
            return {"ok": False, "error": str(exc)}
        return _normalize_body(body_out, r.ok)

    def _get(self, path: str, params=None, include_key: bool = True) -> dict:
        try:
            r = self.session.get(settings.edge_url + path, params=params or {},
                                 headers=self._headers(include_key), timeout=_TIMEOUT)
            body_out = r.json()
        except (requests.RequestException, ValueError) as exc:
            _logger.info("edge unreachable GET %s: %s", path, exc)
            return {"ok": False, "error": str(exc)}
        return _normalize_body(body_out, r.ok)

    # ------------------------------------------------------------------
    def hello(self) -> dict:
        # KHONG gui khoa dang giu (include_key=False): /node/v1/hello la duong
        # DUY NHAT de hoc/hoc lai khoa. Gui kem mot khoa cu/sai se bi tu choi va
        # khoa dung (Odoo da cap) khong bao gio hoc duoc nua - bug thuc te da
        # gap ("sai X-API-Key" lap vinh vien tren chinh /hello).
        res = self._post("/node/v1/hello", {"name": settings.name, "kind": settings.kind},
                         include_key=False)
        if res.get("ok") and res.get("api_key") and res["api_key"] != self.api_key:
            self.store.kv_set("api_key", res["api_key"])
            _logger.info("da nhan/cap nhat api_key tu edge (Odoo da biet thiet bi nay)")
        return res

    def measurements(self, items: list, bid: str, seq: int) -> dict:
        return self._post("/node/v1/measurements", {"items": items, "bid": bid, "seq": seq})

    def heartbeat(self, meta: dict) -> dict:
        return self._post("/node/v1/heartbeat", meta)

    def next_command(self) -> dict:
        return self._get("/node/v1/commands")

    def ack_command(self, cmd_id: int, ok: bool, detail: str = "") -> dict:
        return self._post("/node/v1/commands/ack", {"id": cmd_id, "ok": ok, "detail": detail})
