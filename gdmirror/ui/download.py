"""Live transfer screen: overall bar, per-file bars, speed history, log."""

from __future__ import annotations

import shutil
import time
from collections import deque

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, ProgressBar, RichLog, Static

from ..download import Downloader
from ..drive import Node
from ..util import existing_parent, human

SPARK = "▁▂▃▄▅▆▇█"


class DownloadScreen(Screen):
    BINDINGS = [
        Binding("c", "cancel", "cancel"),
        Binding("escape", "back", "back"),
        Binding("r", "retry", "retry failed"),
    ]

    def __init__(self, files: list[Node]) -> None:
        super().__init__()
        self.files = files
        self.dl: Downloader | None = None
        self.running = False
        self._sample = (time.monotonic(), 0)
        self._rate = 0.0
        self._history: deque[float] = deque(maxlen=48)
        self._failed_nodes: list[Node] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-line")
        yield ProgressBar(total=100, show_eta=False, id="overall")
        yield Static(id="active", classes="panel")
        yield RichLog(id="log", markup=False, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1 / 10, self._tick)
        self._launch(self.files)

    def on_unmount(self) -> None:
        if self.dl is not None:
            self.dl.cancel()

    # -- run --------------------------------------------------------------

    def _launch(self, files: list[Node]) -> None:
        app = self.app
        log = self.query_one("#log", RichLog)

        def on_log(msg: str) -> None:
            self.app.call_from_thread(log.write, msg)

        self.dl = Downloader(
            creds=app.creds,
            dest=app.cfg.dest_path,
            files=files,
            state=app.dlstate,
            workers=app.cfg.workers,
            verify_md5=app.cfg.verify_md5,
            on_log=on_log,
        )
        self.running = True
        self._sample = (time.monotonic(), 0)
        self._history.clear()
        log.write(
            f"queued {len(files)} files, {human(sum(f.size for f in files))}, "
            f"{app.cfg.workers} workers -> {app.cfg.dest_path}"
        )
        self.run_worker(self._worker, thread=True, exclusive=True)

    def _worker(self) -> None:
        assert self.dl is not None
        self.dl.run()
        self.app.call_from_thread(self._finished)

    # -- ui ---------------------------------------------------------------

    def _tick(self) -> None:
        if self.dl is None:
            return
        prog, active = self.dl.snapshot()

        now, (last_t, last_b) = time.monotonic(), self._sample
        if now - last_t >= 0.5:
            self._rate = max(0.0, (prog.done_bytes - last_b) / (now - last_t))
            self._sample = (now, prog.done_bytes)
            if self.running:
                self._history.append(self._rate)

        self.query_one("#overall", ProgressBar).update(
            total=max(prog.total_bytes, 1), progress=prog.done_bytes
        )

        eta = "--:--:--"
        if self._rate > 1 and prog.total_bytes > prog.done_bytes:
            secs = int((prog.total_bytes - prog.done_bytes) / self._rate)
            eta = f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"

        head = Text()
        head.append("  ")
        head.append(f"{prog.done_files}/{prog.total_files}", style="bold #00f5d4")
        head.append(" files   ")
        head.append(f"{human(prog.done_bytes)}", style="#9bff3c")
        head.append(f" / {human(prog.total_bytes)}   ")
        head.append(f"{human(self._rate)}/s", style="bold #9bff3c")
        head.append(f"   eta {eta}   ", style="#4fd6b0")
        head.append_text(self._sparkline())
        head.append("\n  ")
        head.append(f"ok {prog.ok}", style="#9bff3c")
        head.append(f"   skipped {prog.skipped}", style="#3f7a6b")
        head.append(
            f"   failed {prog.failed}",
            style="bold #ff5f7e" if prog.failed else "#3f7a6b",
        )
        free = shutil.disk_usage(existing_parent(self.app.cfg.dest_path)).free
        head.append(f"   free {human(free)}", style="#4fd6b0")
        if not self.running:
            head.append("   [finished]", style="bold #00f5d4")
        self.query_one("#status-line", Static).update(head)

        self.query_one("#active", Static).update(self._active_panel(active))

    def _sparkline(self) -> Text:
        text = Text()
        if not self._history:
            return text
        peak = max(self._history) or 1.0
        for value in self._history:
            text.append(SPARK[min(7, int(value / peak * 7))], style="#0d6e5f")
        return text

    def _active_panel(self, active: list[dict]) -> Text:
        text = Text()
        if not active:
            text.append("idle\n" if not self.running else "waiting for workers...\n",
                        style="#3f7a6b")
            return text
        for entry in sorted(active, key=lambda e: -e["total"])[:8]:
            total = max(entry["total"], 1)
            frac = min(1.0, entry["done"] / total)
            filled = int(frac * 22)
            text.append(f"{int(frac * 100):3d}% ", style="#00f5d4")
            text.append("█" * filled, style="#9bff3c")
            text.append("░" * (22 - filled), style="#0d6e5f")
            text.append(f"  {entry['path'][-58:]}\n", style="#b8ffe4")
        return text

    def _finished(self) -> None:
        self.running = False
        assert self.dl is not None
        prog, _ = self.dl.snapshot()
        self._tick()

        by_path = {n.path: n for n in self.files}
        self._failed_nodes = [by_path[p] for p, _ in prog.failures if p in by_path]

        log = self.query_one("#log", RichLog)
        log.write("")
        log.write(
            f"finished: {prog.ok} downloaded, {prog.skipped} already present, "
            f"{prog.failed} failed"
        )
        if prog.failures:
            log.write("failures:")
            for path, err in prog.failures[:80]:
                log.write(f"  {path} - {err}")
            log.write("press r to retry just the failed files")
        self.notify(
            f"{prog.ok} downloaded · {prog.failed} failed",
            severity="error" if prog.failed else "information",
        )

    # -- actions ----------------------------------------------------------

    def action_cancel(self) -> None:
        if self.dl is not None and self.running:
            self.dl.cancel()
            self.notify("cancelling - partial files are kept for resume")

    def action_retry(self) -> None:
        if self.running:
            self.notify("still running", severity="warning")
            return
        if not self._failed_nodes:
            self.notify("nothing failed", severity="information")
            return
        retry = list(self._failed_nodes)
        self._failed_nodes = []
        self.files = retry
        self._launch(retry)

    def action_back(self) -> None:
        if self.running:
            self.notify("still running - press c to cancel first", severity="warning")
            return
        self.app.pop_screen()
