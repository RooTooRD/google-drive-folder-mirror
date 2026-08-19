"""Parallel, resumable downloader that mirrors the Drive hierarchy on disk."""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from google.auth.transport.requests import AuthorizedSession

from .config import ABORT_MARGIN, CHUNK_SIZE, EXPORT_MAP, WORKERS
from .drive import Node
from .state import State
from .util import md5_of

API = "https://www.googleapis.com/drive/v3/files"


@dataclass
class Progress:
    total_files: int = 0
    total_bytes: int = 0
    done_files: int = 0
    done_bytes: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.done_files >= self.total_files


class Cancelled(Exception):
    pass


class Downloader:
    """Downloads a set of file Nodes into `dest`, mirroring their relative paths.

    Counters live on `self.progress` and the in-flight files on `self.active`;
    both are mutated under a lock so a UI thread can poll them safely instead of
    being flooded with per-chunk callbacks.
    """

    def __init__(
        self,
        creds,
        dest: Path,
        files: list[Node],
        state: State,
        workers: int = WORKERS,
        verify_md5: bool = True,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.creds = creds
        self.dest = dest
        self.files = files
        self.state = state
        self.workers = max(1, workers)
        self.verify_md5 = verify_md5
        self.on_log = on_log

        self.cancel_event = threading.Event()
        self.progress = Progress(
            total_files=len(files), total_bytes=sum(f.size for f in files)
        )
        self.active: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    # -- plumbing ---------------------------------------------------------

    def _session(self) -> AuthorizedSession:
        # requests.Session is not thread safe, so keep one per worker thread.
        session = getattr(self._local, "session", None)
        if session is None:
            session = AuthorizedSession(self.creds)
            self._local.session = session
        return session

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def cancel(self) -> None:
        self.cancel_event.set()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def snapshot(self) -> tuple[Progress, list[dict]]:
        """Thread-safe read of counters plus the currently downloading files."""
        with self._lock:
            p = Progress(
                total_files=self.progress.total_files,
                total_bytes=self.progress.total_bytes,
                done_files=self.progress.done_files,
                done_bytes=self.progress.done_bytes,
                ok=self.progress.ok,
                skipped=self.progress.skipped,
                failed=self.progress.failed,
                failures=list(self.progress.failures),
            )
            active = [dict(v) for v in self.active.values()]
        return p, active

    # -- one file ---------------------------------------------------------

    def target_path(self, node: Node) -> Path:
        rel = node.path
        if node.mime in EXPORT_MAP:
            _, ext = EXPORT_MAP[node.mime]
            if not rel.lower().endswith(ext):
                rel += ext
        return self.dest / rel

    def _adopt(self, node: Node, target: Path) -> bool:
        """Claim a complete file already on disk that has no state record.

        A crash can leave the buffer full of finished downloads whose records were
        never written. Re-fetching them would waste the bandwidth that produced
        them, so verify instead: exact size plus a matching md5 proves the file
        is the one Drive would send. Without an md5 from Drive there is nothing to
        prove it with, so those are re-downloaded rather than trusted.
        """
        if not node.md5 or not target.exists():
            return False
        try:
            if target.stat().st_size != node.size:
                return False
            if md5_of(target) != node.md5:
                return False
        except OSError:
            return False

        self.state.mark(node.key, node.path, node.size, node.md5)
        self._log(f"adopted {node.path} (already complete on disk)")
        return True

    def _fetch(self, node: Node, part: Path, offset: int) -> None:
        """Stream the file body into `part`, starting at `offset`."""
        session = self._session()

        if node.mime in EXPORT_MAP:
            mime, _ = EXPORT_MAP[node.mime]
            url = f"{API}/{node.id}/export"
            params = {"mimeType": mime}
            headers: dict[str, str] = {}
            offset = 0  # export endpoint has no range support
            part.unlink(missing_ok=True)
        else:
            url = f"{API}/{node.id}"
            params = {"alt": "media", "supportsAllDrives": "true"}
            headers = {"Range": f"bytes={offset}-"} if offset else {}

        with session.get(
            url, params=params, headers=headers, stream=True, timeout=60
        ) as resp:
            if resp.status_code == 416:
                return  # server says the range is past EOF: already complete
            resp.raise_for_status()

            if offset and resp.status_code != 206:
                # Range header ignored; restart from zero.
                self._reset_active(node.key, node.size)
                part.unlink(missing_ok=True)
                offset = 0

            with part.open("ab" if offset else "wb") as fh:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    self._check_cancel()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    with self._lock:
                        self.progress.done_bytes += len(chunk)
                        entry = self.active.get(node.key)
                        if entry is not None:
                            entry["done"] += len(chunk)

    def _reset_active(self, key: str, total: int) -> None:
        with self._lock:
            entry = self.active.get(key)
            if entry is not None:
                self.progress.done_bytes -= entry["done"]
                entry["done"] = 0

    def _finish(self, node: Node, outcome: str, error: str = "") -> str:
        with self._lock:
            self.active.pop(node.key, None)
            self.progress.done_files += 1
            if outcome == "ok":
                self.progress.ok += 1
            elif outcome == "skipped":
                self.progress.skipped += 1
                self.progress.done_bytes += node.size
            else:
                self.progress.failed += 1
                self.progress.failures.append((node.path, error))
        return outcome

    def fetch(self, node: Node) -> str:
        """Download a single file. Used by the pipeline, which schedules its own
        work instead of calling run(). Returns 'ok', 'skipped' or 'failed'."""
        return self._download_one(node)

    def _download_one(self, node: Node) -> str:
        """Returns 'ok', 'skipped' or 'failed'."""
        self._check_cancel()
        target = self.target_path(node)
        part = target.with_name(target.name + ".part")

        if self.state.is_done(node.key, node.size, node.md5) and target.exists():
            return self._finish(node, "skipped")

        if self._adopt(node, target):
            return self._finish(node, "skipped")

        target.parent.mkdir(parents=True, exist_ok=True)

        if shutil.disk_usage(self.dest).free < ABORT_MARGIN:
            self._log("!! free space below safety margin - aborting run")
            self.cancel()
            raise Cancelled()

        offset = part.stat().st_size if part.exists() else 0
        with self._lock:
            self.active[node.key] = {
                "path": node.path,
                "done": offset,
                "total": node.size,
            }
            self.progress.done_bytes += offset  # count the resumed head

        try:
            self._fetch(node, part, offset)

            if not part.exists():
                raise FileNotFoundError("server returned no data")

            if self.verify_md5 and node.md5:
                actual = md5_of(part)
                if actual != node.md5:
                    part.unlink(missing_ok=True)
                    raise ValueError(f"md5 mismatch: got {actual}, want {node.md5}")

            part.replace(target)
            self.state.mark(node.key, node.path, target.stat().st_size, node.md5)
            self._log(f"ok  {node.path}")
            return self._finish(node, "ok")

        except Cancelled:
            with self._lock:
                self.active.pop(node.key, None)
            raise
        except Exception as exc:  # network, HTTP, checksum, disk
            msg = f"{type(exc).__name__}: {exc}"
            self._log(f"ERR {node.path} - {msg}")
            return self._finish(node, "failed", msg)

    # -- run --------------------------------------------------------------

    def run(self) -> Progress:
        self.dest.mkdir(parents=True, exist_ok=True)
        # Biggest first: long transfers start early, small ones fill the tail.
        ordered = sorted(self.files, key=lambda n: -n.size)

        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(self._download_one, n) for n in ordered]
                for fut in futures:
                    try:
                        fut.result()
                    except Cancelled:
                        self.cancel()
                        for other in futures:
                            other.cancel()
                        break
                    except Exception as exc:
                        self._log(f"worker error: {exc}")
        finally:
            self.state.save()

        return self.progress
