"""Pipeline screen: Drive → local buffer → Telegram → deleted, live."""

from __future__ import annotations

import shutil
import time
from collections import deque

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, ProgressBar, RichLog, Static

from ..drive import Node
from ..pipeline import Pipeline
from ..util import existing_parent, human

SPARK = "▁▂▃▄▅▆▇█"


class PipelineScreen(Screen):
    BINDINGS = [
        Binding("c", "cancel", "cancel"),
        Binding("i", "index", "publish index"),
        Binding("escape", "back", "back"),
    ]

    def __init__(self, files: list[Node]) -> None:
        super().__init__()
        self.files = files
        self.pipe: Pipeline | None = None
        self.running = False
        self._sample = (time.monotonic(), 0, 0)
        self._rate = 0.0
        self._dl_rate = 0.0
        self._history: deque[float] = deque(maxlen=44)
        self.index_line: Text | None = None   # live index-publish status

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-line")
        yield ProgressBar(total=100, show_eta=False, id="overall")
        yield Static(id="active", classes="panel")
        yield RichLog(id="log", markup=False, max_lines=3000)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1 / 10, self._tick)
        self._start()

    def on_unmount(self) -> None:
        if self.pipe is not None:
            self.pipe.cancel()

    def _start(self) -> None:
        app = self.app
        log = self.query_one("#log", RichLog)

        def on_log(msg: str) -> None:
            self.app.call_from_thread(log.write, msg)

        self.pipe = Pipeline(
            creds=app.creds,
            tg=app.tg,
            chat_id=app.tgcfg.chat_id,
            files=self.files,
            state=app.dlstate,
            dest=app.cfg.dest_path,
            budget_bytes=app.tgcfg.buffer_bytes,
            download_workers=max(1, min(3, app.cfg.workers)),
            purge=app.tgcfg.purge_after_upload,
            throttle=app.tgcfg.throttle_seconds,
            verify_md5=app.cfg.verify_md5,
            index_title=app.drive_tree.name if app.drive_tree else "Drive mirror",
            on_log=on_log,
        )
        self.running = True
        log.write(f"target: {app.tgcfg.chat_title} ({app.tgcfg.chat_id})")
        if not app.tgcfg.purge_after_upload:
            log.write("purge disabled - local copies are kept, disk will fill up")
        self.run_worker(self._worker, thread=True, exclusive=True)

    def _worker(self) -> None:
        assert self.pipe is not None
        self.pipe.run()
        self.app.call_from_thread(self._finished)

    # -- ui ---------------------------------------------------------------

    def _tick(self) -> None:
        if self.pipe is None:
            return
        prog, upload, downloads = self.pipe.snapshot()

        now, (last_t, last_up, last_dl) = time.monotonic(), self._sample
        if now - last_t >= 0.5:
            span = now - last_t
            self._rate = max(0.0, (prog.uploaded_bytes - last_up) / span)
            self._dl_rate = max(0.0, (prog.downloaded_bytes - last_dl) / span)
            self._sample = (now, prog.uploaded_bytes, prog.downloaded_bytes)
            if self.running:
                self._history.append(self._rate)

        self.query_one("#overall", ProgressBar).update(
            total=max(prog.total_bytes, 1), progress=prog.uploaded_bytes
        )

        eta = "--:--:--"
        if self._rate > 1 and prog.total_bytes > prog.uploaded_bytes:
            secs = int((prog.total_bytes - prog.uploaded_bytes) / self._rate)
            eta = f"{secs // 3600:d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"

        free = shutil.disk_usage(existing_parent(self.app.cfg.dest_path)).free
        head = Text()
        head.append("  ")
        head.append(f"{prog.uploaded_files}/{prog.total_files}", style="bold #00f5d4")
        head.append(" sent   ")
        head.append(human(prog.uploaded_bytes), style="#9bff3c")
        head.append(f" / {human(prog.total_bytes)}   ")
        head.append(f"{human(self._rate)}/s", style="bold #9bff3c")
        head.append(f"   eta {eta}   ", style="#4fd6b0")
        head.append_text(self._sparkline())
        head.append("\n  ")
        head.append(f"down {human(self._dl_rate)}/s", style="#4fd6b0")
        full = prog.reserved_bytes >= self.pipe.budget
        head.append(
            f"   buffer {human(prog.reserved_bytes)}/{human(self.pipe.budget)}",
            style="#ffd166" if full else "#4fd6b0",
        )
        if prog.waiting_downloads:
            # Downloaders blocked on the budget is the throttle working, not a fault.
            head.append(
                f" ({prog.waiting_downloads} waiting)", style="#ffd166"
            )
        if prog.stranded_bytes:
            head.append(
                f"   stranded {human(prog.stranded_bytes)}", style="bold #ff5f7e"
            )
        head.append(f"   freed {human(prog.purged_bytes)}", style="#4fd6b0")
        head.append(f"   disk free {human(free)}",
                    style="#9bff3c" if free > 1024**3 else "#ff5f7e")
        head.append(f"   failed {prog.failed}",
                    style="bold #ff5f7e" if prog.failed else "#3f7a6b")
        if prog.flood_until > time.time():
            head.append(
                f"   FLOOD WAIT {int(prog.flood_until - time.time())}s",
                style="bold #ffd166",
            )
        if not self.running:
            head.append("   [finished]", style="bold #00f5d4")
        if self.index_line is not None:
            head.append("\n")
            head.append_text(self.index_line)
        self.query_one("#status-line", Static).update(head)
        self.query_one("#active", Static).update(self._lanes(upload, downloads, prog))

    def _sparkline(self) -> Text:
        text = Text()
        if not self._history:
            return text
        peak = max(self._history) or 1.0
        for value in self._history:
            text.append(SPARK[min(7, int(value / peak * 7))], style="#0d6e5f")
        return text

    def _bar(self, done: int, total: int, width: int = 20) -> Text:
        frac = min(1.0, done / max(total, 1))
        filled = int(frac * width)
        text = Text(f"{int(frac * 100):3d}% ", style="#00f5d4")
        text.append("█" * filled, style="#9bff3c")
        text.append("░" * (width - filled), style="#0d6e5f")
        return text

    def _lanes(self, upload: dict, downloads: list[dict], prog) -> Text:
        text = Text()
        text.append("UPLOADING → TELEGRAM\n", style="bold #00f5d4")
        if upload and upload.get("path"):
            text.append("  ")
            text.append_text(self._bar(upload.get("sent", 0), upload.get("total", 1)))
            text.append(f"  {upload['path'][-56:]}\n", style="#b8ffe4")
        else:
            text.append("  idle\n", style="#3f7a6b")

        text.append("\nDOWNLOADING ← DRIVE\n", style="bold #00f5d4")
        if prog.waiting_downloads and not downloads:
            text.append(
                f"  {prog.waiting_downloads} workers paused - buffer full, "
                "waiting on the uploader\n",
                style="#ffd166",
            )
            return text
        if downloads:
            for entry in sorted(downloads, key=lambda e: -e["total"])[:4]:
                text.append("  ")
                text.append_text(self._bar(entry["done"], entry["total"]))
                text.append(f"  {entry['path'][-56:]}\n", style="#b8ffe4")
        else:
            text.append("  idle\n", style="#3f7a6b")
        return text

    def _finished(self) -> None:
        self.running = False
        assert self.pipe is not None
        prog, _, _ = self.pipe.snapshot()
        self._tick()
        log = self.query_one("#log", RichLog)
        log.write("")
        log.write(
            f"finished: {prog.uploaded_files} uploaded "
            f"({human(prog.uploaded_bytes)}), {human(prog.purged_bytes)} freed "
            f"locally, {prog.failed} failed"
        )
        for path, why in prog.failures[:80]:
            log.write(f"  {path} - {why}")
        if prog.failures:
            log.write("rerun the pipeline to retry the failures")
        log.write("press i to publish and pin the navigable index")
        self.notify(
            f"{prog.uploaded_files} uploaded · {prog.failed} failed",
            severity="error" if prog.failed else "information",
        )

    # -- actions ----------------------------------------------------------

    def action_cancel(self) -> None:
        if self.pipe is not None and self.running:
            self.pipe.cancel()
            self.notify("cancelling after the current file")

    _STAGE = {
        "send": "sending posts",
        "link": "linking back-references",
        "pin": "pinning the parent post",
        "clean": "clearing the old index",
    }

    def action_index(self) -> None:
        if self.running:
            self.notify("wait for the run to finish", severity="warning")
            return
        if self.pipe is None:
            return
        if getattr(self, "_indexing", False):
            self.notify("already publishing the index", severity="warning")
            return
        self._indexing = True
        log = self.query_one("#log", RichLog)
        self.notify("publishing index…")

        def set_line(text: Text) -> None:
            self.index_line = text

        def on_progress(stage: str, done: int, total: int) -> None:
            label = self._STAGE.get(stage, stage)
            line = Text("  ⏳ index · ", style="#ffd166")
            line.append(label, style="bold #ffd166")
            if total > 1:
                line.append(f"  {done}/{total}", style="#ffd166")
            self.app.call_from_thread(set_line, line)

        def work() -> None:
            call = self.app.call_from_thread
            try:
                result = self.pipe.publish_index(on_progress=on_progress)
            except Exception as exc:
                fail = Text("  ✖ index failed: ", style="bold #ff5f7e")
                fail.append(f"{type(exc).__name__}: {exc}", style="#ff5f7e")
                call(set_line, fail)
                call(log.write, f"index failed: {type(exc).__name__}: {exc}")
                call(self.notify, "index failed", severity="error")
                return
            finally:
                self._indexing = False

            posts, link = result["posts"], result["root_link"]
            done = Text("  ✅ index published · ", style="bold #9bff3c")
            done.append(f"{posts} posts · pinned", style="#9bff3c")
            call(set_line, done)
            call(log.write, f"index published and pinned ({posts} posts): {link}")
            call(self.notify, f"index published · {posts} posts pinned")

        self.run_worker(work, thread=True, exclusive=True)

    def action_back(self) -> None:
        if self.running:
            self.notify("still running - press c to cancel first", severity="warning")
            return
        self.app.pop_screen()
