"""A local stand-in for Drive: an HTTP server that speaks Range requests."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gdmirror.config import FOLDER_MIME
from gdmirror.drive import Node

BLOBS: dict[str, bytes] = {}
SLOW: set[str] = set()  # ids the server dribbles out, to test cancelling mid-flight
HITS: dict[str, int] = {}  # id -> number of GETs served, to prove nothing refetched


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    def do_GET(self) -> None:
        file_id = self.path.split("?")[0].rstrip("/").split("/")[-1]
        body = BLOBS.get(file_id)
        if body is None:
            self.send_error(404)
            return
        HITS[file_id] = HITS.get(file_id, 0) + 1

        start = 0
        rng = self.headers.get("Range")
        if rng:
            match = re.match(r"bytes=(\d+)-", rng)
            if match:
                start = int(match.group(1))
            if start >= len(body):
                self.send_response(416)
                self.end_headers()
                return

        chunk = body[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(chunk)))
        if rng:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
        self.end_headers()
        if file_id in SLOW:
            for i in range(0, len(chunk), 32_768):
                try:
                    self.wfile.write(chunk[i : i + 32_768])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.005)
        else:
            self.wfile.write(chunk)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/files"


def make_file(node_id: str, path: str, size: int) -> Node:
    body = os.urandom(size)
    BLOBS[node_id] = body
    return Node(
        id=node_id,
        name=Path(path).name,
        mime="video/mp4",
        size=len(body),
        md5=hashlib.md5(body).hexdigest(),
        modified="",
        path=path,
    )


def make_folder(name: str, path: str = "") -> Node:
    return Node(id=f"folder-{name}", name=name, mime=FOLDER_MIME, size=0, md5=None,
                modified="", path=path or name)


def patched_session(dl) -> None:
    """Swap Telethon-free plain requests sessions into a Downloader."""
    import requests

    dl._session = lambda: requests.Session()  # type: ignore[method-assign]
