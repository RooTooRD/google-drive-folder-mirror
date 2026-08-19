"""Telegram credentials and target chat, kept out of settings.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .config import ROOT

TG_CONFIG_FILE = ROOT / "telegram.json"
SESSION_FILE = ROOT / "telegram.session"
MANIFEST_FILE = ROOT / "manifest.json"


@dataclass
class TgConfig:
    api_id: int = 0
    api_hash: str = ""
    chat_id: int = 0
    chat_title: str = ""
    buffer_gb: float = 3.0
    purge_after_upload: bool = True
    throttle_seconds: float = 1.0

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def has_target(self) -> bool:
        return bool(self.chat_id)

    @property
    def buffer_bytes(self) -> int:
        return int(self.buffer_gb * 1024**3)

    @classmethod
    def load(cls) -> "TgConfig":
        if not TG_CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(TG_CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        TG_CONFIG_FILE.write_text(json.dumps(asdict(self), indent=1))
        TG_CONFIG_FILE.chmod(0o600)
