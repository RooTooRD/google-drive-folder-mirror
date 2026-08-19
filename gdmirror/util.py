"""Small shared helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_ILLEGAL = re.compile(r"[/\\\x00-\x1f]")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def sanitize(name: str) -> str:
    """Make a Drive file name safe as a single path component."""
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(". ")
    if cleaned in {"", ".", ".."}:
        cleaned = "unnamed"
    # Leave room for a ".part" suffix inside the 255-byte ext4 limit.
    while len(cleaned.encode("utf-8")) > 240:
        cleaned = cleaned[:-1]
    return cleaned


def dedupe(name: str, taken: set[str]) -> str:
    """Return a sibling-unique variant of `name`."""
    if name not in taken:
        taken.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    i = 2
    while True:
        candidate = f"{stem} ({i}){'.' + ext if ext else ''}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        i += 1


def existing_parent(path: Path) -> Path:
    """Nearest existing ancestor, so disk_usage works before dest is created."""
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def md5_of(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
