"""Persistent progress: what is on disk, and what has reached Telegram.

Two independent facts per file:
  done      - a verified copy exists locally right now
  uploaded  - it has been sent to Telegram (survives deleting the local copy)
The pipeline clears `done` when it purges a local file, so `done` always means
"on disk", while `uploaded` is the permanent record.
"""

from __future__ import annotations

import json
import threading

from .config import STATE_FILE


class State:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done: dict[str, dict] = {}
        self._uploaded: dict[str, dict] = {}
        self._index_messages: list[int] = []
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self._done = data.get("done", {})
        self._uploaded = data.get("uploaded", {})
        self._index_messages = list(data.get("index_messages", []))

    def save(self) -> None:
        with self._lock:
            payload = {
                "done": dict(self._done),
                "uploaded": dict(self._uploaded),
                "index_messages": list(self._index_messages),
            }
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(STATE_FILE)

    # -- local copies -----------------------------------------------------

    def is_done(self, key: str, size: int, md5: str | None) -> bool:
        with self._lock:
            rec = self._done.get(key)
        if not rec:
            return False
        if md5 and rec.get("md5"):
            return rec["md5"] == md5
        return rec.get("size") == size

    def mark(self, key: str, path: str, size: int, md5: str | None) -> None:
        with self._lock:
            self._done[key] = {"path": path, "size": size, "md5": md5}

    def forget(self, key: str) -> None:
        with self._lock:
            self._done.pop(key, None)

    def __len__(self) -> int:
        return len(self._done)

    # -- index posts ------------------------------------------------------

    def index_messages(self) -> list[int]:
        with self._lock:
            return list(self._index_messages)

    def set_index_messages(self, ids: list[int]) -> None:
        with self._lock:
            self._index_messages = list(ids)

    # -- telegram ---------------------------------------------------------

    def is_uploaded(self, key: str) -> bool:
        with self._lock:
            return key in self._uploaded

    def upload_record(self, key: str) -> dict | None:
        with self._lock:
            rec = self._uploaded.get(key)
        return dict(rec) if rec else None

    def mark_uploaded(
        self, key: str, path: str, size: int, chat_id: int, msg_id: int, link: str
    ) -> None:
        with self._lock:
            self._uploaded[key] = {
                "path": path,
                "size": size,
                "chat_id": chat_id,
                "msg_id": msg_id,
                "link": link,
            }

    def forget_upload(self, key: str) -> None:
        with self._lock:
            self._uploaded.pop(key, None)

    @property
    def uploaded_count(self) -> int:
        return len(self._uploaded)

    @property
    def uploaded_bytes(self) -> int:
        with self._lock:
            return sum(r.get("size", 0) for r in self._uploaded.values())

    def uploads(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._uploaded.values()]
