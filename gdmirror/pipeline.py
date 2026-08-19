"""Download → upload → delete pipeline.

Files move through a disk budget that is never exceeded: a downloader pool
reserves space before fetching, a single uploader consumes files in strict tree
order so the Telegram channel reads like the folder, and the local copy is
deleted only after Telegram confirms the message and the size matches.

Everything is resumable. A file that already has an upload record is skipped
outright, so killing the app mid-run costs at most one file's work.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import ABORT_MARGIN
from .download import Downloader
from .drive import Node
from .state import State
from .tg import TG, TelegramError
from .tgconfig import MANIFEST_FILE
from .util import human


@dataclass
class PipeProgress:
    total_files: int = 0
    total_bytes: int = 0
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    failed: int = 0
    purged_bytes: int = 0
    reserved_bytes: int = 0
    # Bytes still on disk for files that failed: they keep holding budget, so a
    # run with failures cannot quietly overspend the buffer.
    stranded_bytes: int = 0
    waiting_downloads: int = 0
    downloaded_bytes: int = 0
    flood_until: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        creds,
        tg: TG,
        chat_id: int,
        files: list[Node],
        state: State,
        dest: Path,
        budget_bytes: int,
        download_workers: int = 2,
        purge: bool = True,
        throttle: float = 1.0,
        verify_md5: bool = True,
        index_title: str = "Drive mirror",
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.tg = tg
        self.chat_id = chat_id
        self.index_title = index_title
        self.state = state
        self.dest = dest
        self.budget = max(budget_bytes, 1)
        self.purge = purge
        self.throttle = throttle
        self.on_log = on_log

        # Anything already in Telegram is finished business.
        self.queue = [f for f in files if not state.is_uploaded(f.key)]
        self.skipped_existing = len(files) - len(self.queue)

        self.downloader = Downloader(
            creds=creds,
            dest=dest,
            files=self.queue,
            state=state,
            workers=download_workers,
            verify_md5=verify_md5,
            on_log=on_log,
        )
        self.dl_workers = max(1, download_workers)

        self.cancel_event = threading.Event()
        self.progress = PipeProgress(
            total_files=len(self.queue),
            total_bytes=sum(f.size for f in self.queue),
        )
        self.active_upload: dict | None = None

        self._cv = threading.Condition()
        self._claim = 0                       # next index a downloader may take
        self._reserve_turn = 0                # next index allowed to take disk
        self._next = 0                        # next index the uploader wants
        self._ready: dict[int, Node] = {}     # downloaded, waiting to upload
        self._failed: set[int] = set()
        self._reserved = 0
        self._stranded = 0
        self._waiting = 0
        self._warned_stranded = False
        self._manifest: dict[str, dict] = _load_manifest()

    # -- helpers ----------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def cancel(self) -> None:
        self.cancel_event.set()
        self.downloader.cancel()
        with self._cv:
            self._cv.notify_all()

    def snapshot(self) -> tuple[PipeProgress, dict, list[dict]]:
        dl_prog, dl_active = self.downloader.snapshot()
        with self._cv:
            self.progress.reserved_bytes = self._reserved
            prog = PipeProgress(
                total_files=self.progress.total_files,
                total_bytes=self.progress.total_bytes,
                uploaded_files=self.progress.uploaded_files,
                uploaded_bytes=self.progress.uploaded_bytes,
                failed=self.progress.failed,
                purged_bytes=self.progress.purged_bytes,
                reserved_bytes=self._reserved,
                stranded_bytes=self._stranded,
                waiting_downloads=self._waiting,
                downloaded_bytes=dl_prog.done_bytes,
                flood_until=self.progress.flood_until,
                failures=list(self.progress.failures),
            )
            upload = dict(self.active_upload) if self.active_upload else {}
        return prog, upload, dl_active

    # -- download side ----------------------------------------------------

    def _reserve(self, index: int, size: int) -> bool:
        """Take `size` out of the disk budget for queue position `index`.

        Reservations are granted in queue order. That matters: the uploader can
        only advance on `_next`, so if a later file were allowed to take disk
        first it could starve `_next` of budget while itself waiting to be
        uploaded — every thread blocked on the other. Ordering the grants makes
        that impossible, because the file the uploader is waiting for always
        holds disk already.

        Returns False if cancelled.
        """
        with self._cv:
            while self._reserve_turn != index and not self.cancel_event.is_set():
                self._cv.wait(0.25)
            self._waiting += 1
            try:
                while not self.cancel_event.is_set():
                    # A file larger than the whole budget still runs, alone.
                    if self._reserved == 0 or self._reserved + size <= self.budget:
                        self._reserved += size
                        self._reserve_turn = index + 1
                        self._cv.notify_all()
                        return True
                    self._cv.wait(0.25)
            finally:
                self._waiting -= 1
            # Cancelled: hand the turn on so no one waits on a dead index.
            self._reserve_turn = max(self._reserve_turn, index + 1)
            self._cv.notify_all()
            return False

    def _on_disk_bytes(self, node: Node) -> int:
        """Bytes this node currently occupies in the buffer, complete or partial."""
        target = self.downloader.target_path(node)
        total = 0
        for candidate in (target, target.with_name(target.name + ".part")):
            try:
                total += candidate.stat().st_size
            except OSError:
                pass
        return total

    def _release_slot(self, node: Node, size: int) -> None:
        """Give the node's budget back, minus whatever it left on disk.

        A failed download keeps its `.part` for resume, and a failed upload keeps
        the whole file. Releasing the full reservation in those cases would let
        the buffer grow past the budget invisibly, one failure at a time, until
        the disk-space abort fires. So bytes still present stay reserved and get
        reported as stranded.
        """
        remaining = min(self._on_disk_bytes(node), size) if self.purge else 0
        with self._cv:
            self._reserved = max(0, self._reserved - (size - remaining))
            self._stranded += remaining
            stranded = self._stranded
            self._cv.notify_all()

        if remaining and stranded > self.budget * 0.4 and not self._warned_stranded:
            self._warned_stranded = True
            self._log(
                f"!! {human(stranded)} of the {human(self.budget)} buffer is held by "
                "failed files. Clear them from the buffer directory or raise the "
                "buffer, or the run will slow to a stop."
            )

    def _dl_worker(self) -> None:
        while not self.cancel_event.is_set():
            with self._cv:
                if self._claim >= len(self.queue):
                    return
                index = self._claim
                self._claim += 1
            node = self.queue[index]

            if not self._reserve(index, node.size):
                return

            outcome = self.downloader.fetch(node)
            if outcome == "failed":
                self._release_slot(node, node.size)
                with self._cv:
                    self._failed.add(index)
                    self.progress.failed += 1
                    self.progress.failures.append((node.path, "download failed"))
                    self._cv.notify_all()
                continue

            with self._cv:
                self._ready[index] = node
                self._cv.notify_all()

    # -- upload side ------------------------------------------------------

    def _caption(self, node: Node) -> str:
        parent = str(Path(node.path).parent)
        parent = "" if parent == "." else parent
        lines = [node.name]
        if parent:
            lines.append(parent)
        lines.append(human(node.size))
        return "\n".join(lines)[:1024]

    def _upload_loop(self) -> None:
        while not self.cancel_event.is_set():
            with self._cv:
                while (
                    self._next < len(self.queue)
                    and self._next not in self._ready
                    and self._next not in self._failed
                    and not self.cancel_event.is_set()
                ):
                    self._cv.wait(0.25)
                if self.cancel_event.is_set() or self._next >= len(self.queue):
                    return
                index = self._next
                if index in self._failed:
                    self._next += 1
                    continue
                node = self._ready.pop(index)

            self._upload_one(node)

            with self._cv:
                self._next += 1
                self._cv.notify_all()

            if self.throttle and not self.cancel_event.is_set():
                time.sleep(self.throttle)

    def _upload_one(self, node: Node) -> None:
        path = self.downloader.target_path(node)
        if not path.exists():
            self._fail(node, "local file vanished before upload")
            self._release_slot(node, node.size)
            return

        local_size = path.stat().st_size

        def on_progress(sent: int, total: int) -> None:
            with self._cv:
                self.active_upload = {
                    "path": node.path,
                    "sent": sent,
                    "total": total or local_size,
                }

        def on_flood(seconds: int) -> None:
            with self._cv:
                self.progress.flood_until = time.time() + seconds
            self._log(f"flood wait {seconds}s (telegram rate limit)")

        with self._cv:
            self.active_upload = {"path": node.path, "sent": 0, "total": local_size}

        try:
            result = self.tg.upload(
                path=path,
                chat_id=self.chat_id,
                caption=self._caption(node),
                progress=on_progress,
                cancel=self.cancel_event,
                on_flood=on_flood,
            )
        except TelegramError as exc:
            self._fail(node, str(exc))
            self._release_slot(node, node.size)
            with self._cv:
                self.active_upload = None
            return
        except Exception as exc:
            self._fail(node, f"{type(exc).__name__}: {exc}")
            self._release_slot(node, node.size)
            with self._cv:
                self.active_upload = None
            return

        # Deleting is irreversible, so re-check the size here rather than trusting
        # the client layer to have done it.
        if result["size"] != local_size:
            self._fail(
                node,
                f"size mismatch: local {local_size}, telegram {result['size']}",
            )
            self._release_slot(node, node.size)
            with self._cv:
                self.active_upload = None
            return

        self.state.mark_uploaded(
            node.key, node.path, result["size"], self.chat_id,
            result["msg_id"], result["link"],
        )
        self._manifest[node.path] = {
            "size": result["size"],
            "msg_id": result["msg_id"],
            "link": result["link"],
            "md5": node.md5,
        }

        # Only now is deleting the local copy safe: the message exists and the
        # size Telegram reports matches what we sent.
        purged = 0
        if self.purge:
            try:
                path.unlink()
                purged = local_size
                self.state.forget(node.key)
            except OSError as exc:
                self._log(f"could not delete {path.name}: {exc}")

        with self._cv:
            self.progress.uploaded_files += 1
            self.progress.uploaded_bytes += result["size"]
            self.progress.purged_bytes += purged
            self.progress.flood_until = 0.0
            self.active_upload = None

        self._release_slot(node, node.size)
        self._log(f"sent {node.path}")

        # Save after every single upload, not in batches. The local copy is
        # already gone at this point, so an unsaved record means the file exists
        # only in Telegram with nothing pointing at it - a later run re-uploads
        # it and the channel gets duplicates. One small atomic write per file,
        # minutes apart, is nothing next to that.
        self.state.save()
        _save_manifest(self._manifest)

    def _fail(self, node: Node, why: str) -> None:
        with self._cv:
            self.progress.failed += 1
            self.progress.failures.append((node.path, why))
        self._log(f"FAILED {node.path} - {why}")

    # -- run --------------------------------------------------------------

    def run(self) -> PipeProgress:
        self.dest.mkdir(parents=True, exist_ok=True)
        self._clamp_budget()
        if self.skipped_existing:
            self._log(f"{self.skipped_existing} files already in telegram, skipping")
        self._log(
            f"queued {len(self.queue)} files, {human(self.progress.total_bytes)}, "
            f"disk budget {human(self.budget)}"
        )

        threads = [
            threading.Thread(target=self._dl_worker, daemon=True, name=f"dl{i}")
            for i in range(self.dl_workers)
        ]
        uploader = threading.Thread(target=self._upload_loop, daemon=True, name="up")
        for t in threads:
            t.start()
        uploader.start()

        try:
            for t in threads:
                t.join()
            # Downloads are finished; let the uploader drain what is on disk.
            with self._cv:
                self._cv.notify_all()
            uploader.join()
        finally:
            self.state.save()
            _save_manifest(self._manifest)

        # snapshot(), not self.progress: the derived fields (stranded, reserved,
        # waiting, downloaded) only exist on a snapshot.
        prog, _, _ = self.snapshot()
        return prog

    def _clamp_budget(self) -> None:
        """Never let the configured buffer exceed what the disk can actually give.

        Files already sitting in the buffer count as spent, so a resumed run does
        not budget for space that is occupied.
        """
        if not self.purge:
            return
        import shutil

        free = shutil.disk_usage(self.dest).free
        headroom = free - ABORT_MARGIN
        if headroom < self.budget:
            if headroom <= 0:
                self._log(
                    f"!! only {human(free)} free, below the {human(ABORT_MARGIN)} "
                    "safety margin - free some disk before running"
                )
                self.budget = 1
                return
            self._log(
                f"buffer trimmed from {human(self.budget)} to {human(headroom)} "
                f"({human(free)} free on disk)"
            )
            self.budget = headroom

        existing = sum(
            f.stat().st_size for f in self.dest.rglob("*") if f.is_file()
        )
        if existing:
            self._log(f"{human(existing)} already in the buffer directory")

    # -- index ------------------------------------------------------------

    def build_index(self) -> str:
        """Markdown index of everything uploaded so far, grouped by folder."""
        records = sorted(self.state.uploads(), key=lambda r: r["path"])
        lines = [
            f"# {self.index_title}",
            "",
            f"{len(records)} files · {human(sum(r.get('size', 0) for r in records))}",
            "",
        ]
        current = None
        for rec in records:
            folder = str(Path(rec["path"]).parent)
            folder = "/" if folder == "." else folder
            if folder != current:
                current = folder
                lines += ["", f"## {folder}", ""]
            name = Path(rec["path"]).name
            lines.append(
                f"- [{name}]({rec['link']}) — {human(rec.get('size', 0))}"
            )
        return "\n".join(lines) + "\n"

    def publish_index(self) -> str:
        """Upload the index as a pinned document. Returns the message link."""
        index_path = self.dest / "INDEX.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(self.build_index())
        result = self.tg.upload(
            path=index_path,
            chat_id=self.chat_id,
            caption="INDEX — every file in this channel, with links",
        )
        self.tg.pin(self.chat_id, result["msg_id"])
        index_path.unlink(missing_ok=True)
        return result["link"]


def _load_manifest() -> dict[str, dict]:
    if not MANIFEST_FILE.exists():
        return {}
    try:
        return json.loads(MANIFEST_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest: dict[str, dict]) -> None:
    tmp = MANIFEST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    tmp.replace(MANIFEST_FILE)
