"""Drive API: recursive tree walk, caching, and the Node model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Iterator

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import EXPORT_MAP, EXPORT_SIZE_GUESS, FOLDER_MIME, SHORTCUT_MIME, TREE_CACHE
from .util import dedupe, sanitize

LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, size, md5Checksum, "
    "modifiedTime, shortcutDetails)"
)


@dataclass
class Node:
    id: str
    name: str          # sanitized, sibling-unique
    mime: str
    size: int          # bytes; 0 for folders, guess for Google-native docs
    md5: str | None
    modified: str
    path: str          # relative path from the root folder
    children: list["Node"] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return self.mime == FOLDER_MIME

    @property
    def key(self) -> str:
        """Stable identity. Includes the path so a file linked into two folders
        is tracked (and downloaded) once per location."""
        return f"{self.id}:{self.path}"

    @property
    def is_native(self) -> bool:
        return self.mime in EXPORT_MAP

    @property
    def size_known(self) -> bool:
        return not self.is_folder and not self.is_native

    def walk_files(self) -> Iterator["Node"]:
        """Yield every non-folder descendant, self included if it is a file."""
        if not self.is_folder:
            yield self
            return
        for child in self.children:
            yield from child.walk_files()

    def total_size(self) -> int:
        return sum(f.size for f in self.walk_files())

    def count_files(self) -> int:
        return sum(1 for _ in self.walk_files())

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "md5": self.md5,
            "modified": self.modified,
            "path": self.path,
        }
        if self.is_folder:
            d["children"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        node = cls(
            id=d["id"],
            name=d["name"],
            mime=d["mime"],
            size=d["size"],
            md5=d.get("md5"),
            modified=d.get("modified", ""),
            path=d["path"],
        )
        node.children = [cls.from_dict(c) for c in d.get("children", [])]
        return node


class DriveClient:
    def __init__(self, creds):
        self.creds = creds
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    def get_folder(self, folder_id: str) -> dict:
        return (
            self.svc.files()
            .get(fileId=folder_id, fields="id, name, mimeType", supportsAllDrives=True)
            .execute()
        )

    def list_children(self, folder_id: str) -> list[dict]:
        out: list[dict] = []
        page = None
        while True:
            resp = (
                self.svc.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=LIST_FIELDS,
                    pageSize=1000,
                    pageToken=page,
                    orderBy="folder,name_natural",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            out.extend(resp.get("files", []))
            page = resp.get("nextPageToken")
            if not page:
                return out

    def resolve_shortcut(self, meta: dict) -> dict | None:
        """Follow a shortcut to its target metadata, or None if it is broken."""
        target_id = (meta.get("shortcutDetails") or {}).get("targetId")
        if not target_id:
            return None
        try:
            target = (
                self.svc.files()
                .get(
                    fileId=target_id,
                    fields="id, name, mimeType, size, md5Checksum, modifiedTime",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError:
            return None
        target["name"] = meta["name"]  # keep the shortcut's visible name
        return target

    def build_tree(
        self,
        folder_id: str,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> Node:
        """Walk the whole folder. on_progress(current_path, files_seen_so_far)."""
        meta = self.get_folder(folder_id)
        if meta["mimeType"] != FOLDER_MIME:
            raise ValueError(f"{folder_id} is not a folder ({meta['mimeType']})")

        root = Node(
            id=meta["id"],
            name=sanitize(meta["name"]),
            mime=FOLDER_MIME,
            size=0,
            md5=None,
            modified="",
            path="",
        )
        seen = 0
        visiting: set[str] = set()

        def recurse(node: Node) -> None:
            nonlocal seen
            if node.id in visiting:  # shortcut cycle guard
                return
            visiting.add(node.id)
            if on_progress:
                on_progress(node.path or node.name, seen)

            taken: set[str] = set()
            for meta in self.list_children(node.id):
                if meta["mimeType"] == SHORTCUT_MIME:
                    resolved = self.resolve_shortcut(meta)
                    if resolved is None:
                        continue
                    meta = resolved

                name = dedupe(sanitize(meta["name"]), taken)
                path = f"{node.path}/{name}" if node.path else name
                is_folder = meta["mimeType"] == FOLDER_MIME

                if is_folder:
                    size = 0
                elif meta["mimeType"] in EXPORT_MAP:
                    size = EXPORT_SIZE_GUESS
                else:
                    size = int(meta.get("size", 0))

                child = Node(
                    id=meta["id"],
                    name=name,
                    mime=meta["mimeType"],
                    size=size,
                    md5=meta.get("md5Checksum"),
                    modified=meta.get("modifiedTime", ""),
                    path=path,
                )
                node.children.append(child)
                if is_folder:
                    recurse(child)
                else:
                    seen += 1

            visiting.discard(node.id)

        recurse(root)
        if on_progress:
            on_progress("done", seen)
        return root


def save_tree(root: Node, folder_id: str) -> None:
    TREE_CACHE.write_text(
        json.dumps({"folder_id": folder_id, "root": root.to_dict()}, indent=1)
    )


def load_tree(folder_id: str) -> Node | None:
    if not TREE_CACHE.exists():
        return None
    try:
        data = json.loads(TREE_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("folder_id") != folder_id:
        return None
    return Node.from_dict(data["root"])
