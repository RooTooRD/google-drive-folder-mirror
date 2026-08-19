"""Paths, constants and tunables."""

from __future__ import annotations

import os
from pathlib import Path

# The Drive folder to mirror. Set it in the Settings screen, or here via the
# environment / --folder-id. Deliberately empty by default: a hard-coded id
# would ship a pointer to someone else's private folder.
DEFAULT_FOLDER_ID = os.environ.get("GDM_FOLDER_ID", "")

ROOT = Path(__file__).resolve().parent.parent

CREDENTIALS_FILE = Path(os.environ.get("GDM_CREDENTIALS", ROOT / "credential.json"))
TOKEN_FILE = Path(os.environ.get("GDM_TOKEN", ROOT / "token.json"))
TREE_CACHE = ROOT / "tree.json"
STATE_FILE = ROOT / "state.json"
SETTINGS_FILE = ROOT / "settings.json"
DEST_DIR = Path(os.environ.get("GDM_DEST", ROOT / "downloads"))

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Google-native docs have no binary; they must be exported.
EXPORT_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
}
# Rough guess used for space math only; export size is unknown up front.
EXPORT_SIZE_GUESS = 2 * 1024 * 1024

WORKERS = int(os.environ.get("GDM_WORKERS", "4"))
CHUNK_SIZE = 1024 * 1024
# Refuse to start (and abort mid-run) if free space would drop below this.
SPACE_MARGIN = 2 * 1024**3
ABORT_MARGIN = 512 * 1024**2
