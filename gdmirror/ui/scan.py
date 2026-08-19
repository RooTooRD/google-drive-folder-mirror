"""Scan screen: walks the Drive folder and caches the tree."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static

from ..drive import DriveClient, save_tree
from ..util import human
from .common import pulse_bar, spinner


class ScanScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("b", "back", "back", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-line")
        yield RichLog(id="log", markup=False, max_lines=400)
        yield Footer()

    def on_mount(self) -> None:
        self._t0 = time.monotonic()
        self._seen = 0
        self._where = ""
        self._phase = "scanning"
        self._result = ""
        self.set_interval(1 / 15, self._pulse)
        self.run_worker(self._scan_worker, thread=True, exclusive=True)

    def _pulse(self) -> None:
        text = Text()
        if self._phase == "scanning":
            text.append(f"  {spinner()}  ", style="#00f5d4")
            text.append("walking folder tree", style="bold")
            text.append(f"   {self._seen} files", style="#9bff3c")
            text.append(f"   {int(time.monotonic() - self._t0)}s\n", style="#3f7a6b")
            text.append("  ")
            text.append_text(pulse_bar(width=40))
            text.append(f"  {self._where[-48:]}", style="#3f7a6b")
        elif self._phase == "ok":
            text.append("  ✔  ", style="bold #9bff3c")
            text.append(self._result, style="#b8ffe4")
        else:
            text.append("  ✖  ", style="bold #ff5f7e")
            text.append(self._result, style="#ff5f7e")
        self.query_one("#status-line", Static).update(text)

    def _scan_worker(self) -> None:
        app = self.app
        call = app.call_from_thread
        log = self.query_one("#log", RichLog)

        def progress(path: str, seen: int) -> None:
            self._seen = seen
            self._where = path
            call(log.write, f"  {path}")

        try:
            client = DriveClient(app.creds)
            root = client.build_tree(app.cfg.folder_id, on_progress=progress)
        except Exception as exc:
            self._phase = "error"
            self._result = f"{type(exc).__name__}: {exc}"
            call(log.write, f"ERROR: {self._result}")
            return

        save_tree(root, app.cfg.folder_id)
        files = list(root.walk_files())
        self._phase = "ok"
        self._result = (
            f"{root.name}: {len(files)} files, {human(sum(f.size for f in files))} "
            f"in {time.monotonic() - self._t0:.1f}s"
        )
        call(self._finish, root)

    def _finish(self, root) -> None:
        self.app.drive_tree = root
        self.query_one("#log", RichLog).write("")
        self.query_one("#log", RichLog).write("tree cached to tree.json")
        self.notify("scan complete")
        self.set_timer(1.0, self.action_back)

    def action_back(self) -> None:
        if self.is_attached:
            self.app.pop_screen()
