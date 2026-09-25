# -*- coding: utf-8 -*-
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Duong dan .env CHIA SE voi settings_api.py (trang /setup) - phai cung MOT
# cach resolve, khong de moi noi tu doan lay path rieng (se lech nhau tuy CWD
# luc start process) - xem review 2026-09-25 (Docker env_file: chi inject
# 1 lan luc container CREATE, KHONG mount file; .env phai duoc BIND-MOUNT +
# load_dotenv(DOTENV_PATH, override=True) moi thay doi tu web UI co hieu luc
# that qua lan restart tiep theo - xem docker-compose.yml). Copy pattern tu
# ../edge_collector/edge_collector/config.py (da giai quyet dung bai toan nay).
DOTENV_PATH = Path(find_dotenv(usecwd=True) or (Path(__file__).resolve().parent.parent / ".env"))

load_dotenv(DOTENV_PATH, override=True)


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    edge_url: str = os.environ.get("NODE_EDGE_URL", "http://127.0.0.1:8000").rstrip("/")
    serial: str = os.environ.get("NODE_SERIAL", "NODE-01")
    name: str = os.environ.get("NODE_NAME", "")
    kind: str = os.environ.get("NODE_KIND", "other")
    channels_file: str = os.environ.get("NODE_CHANNELS_FILE", "./channels.json")
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("NODE_STATE_DIR", "./var")))

    hello_interval_s: int = _int("NODE_HELLO_INTERVAL_S", 60)
    heartbeat_interval_s: int = _int("NODE_HEARTBEAT_INTERVAL_S", 30)
    submit_interval_s: float = float(os.environ.get("NODE_SUBMIT_INTERVAL_S", "2"))
    command_poll_interval_s: float = float(os.environ.get("NODE_COMMAND_POLL_INTERVAL_S", "2"))

    setup_port: int = _int("NODE_SETUP_PORT", 8091)
    setup_token: str = os.environ.get("NODE_SETUP_TOKEN", "")

    def __post_init__(self):
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def state_json_path(self) -> Path:
        return self.state_dir / "node_state.json"

    @property
    def sqlite_path(self) -> Path:
        return self.state_dir / "node_agent.db"

    def load_channels(self) -> list:
        path = Path(self.channels_file)
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            return json.load(f)


settings = Settings()
