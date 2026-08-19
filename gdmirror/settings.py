"""User-editable settings, persisted between runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import DEFAULT_FOLDER_ID, DEST_DIR, SETTINGS_FILE, WORKERS


@dataclass
class Settings:
    folder_id: str = DEFAULT_FOLDER_ID
    dest: str = str(DEST_DIR)
    workers: int = WORKERS
    verify_md5: bool = True

    @property
    def dest_path(self) -> Path:
        return Path(self.dest).expanduser()

    @classmethod
    def load(cls) -> "Settings":
        if not SETTINGS_FILE.exists():
            return cls()
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=1))
