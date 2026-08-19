"""Verify screen: re-hash the local mirror and report drift from Drive."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, ProgressBar, RichLog, Static

from ..config import EXPORT_MAP
from ..drive import Node
from ..util import human, md5_of
from .common import spinner


class VerifyScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("c", "cancel", "cancel"),
        Binding("f", "forget", "requeue bad files"),
    ]

    def __init__(self, root: Node) -> None:
        super().__init__()
        self.root = root
        self.running = True
        self.cancelled = False
        self.checked = 0
        self.ok = 0
        self.missing: list[Node] = []
        self.corrupt: list[Node] = []
        self.unverifiable = 0
        self._total = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-line")
        yield ProgressBar(total=100, show_eta=False, id="overall")
        yield RichLog(id="log", markup=False, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self._t0 = time.monotonic()
        self.set_interval(1 / 10, self._tick)
        self.run_worker(self._worker, thread=True, exclusive=True)

    def _target(self, node: Node) -> object:
        rel = node.path
        if node.mime in EXPORT_MAP:
            _, ext = EXPORT_MAP[node.mime]
            if not rel.lower().endswith(ext):
                rel += ext
        return self.app.cfg.dest_path / rel

    def _worker(self) -> None:
        call = self.app.call_from_thread
        log = self.query_one("#log", RichLog)
        state = self.app.dlstate

        candidates = [
            n for n in self.root.walk_files()
            if state.is_done(n.key, n.size, n.md5)
        ]
        self._total = len(candidates)
        call(log.write, f"verifying {len(candidates)} recorded files")

        for node in candidates:
            if self.cancelled:
                break
            path = self._target(node)
            self.checked += 1
            if not path.exists():
                self.missing.append(node)
                call(log.write, f"MISSING  {node.path}")
                continue
            if not node.md5:
                self.unverifiable += 1
                continue
            if md5_of(path) != node.md5:
                self.corrupt.append(node)
                call(log.write, f"CORRUPT  {node.path}")
                continue
            self.ok += 1

        self.running = False
        call(self._finish)

    def _tick(self) -> None:
        text = Text()
        if self.running:
            text.append(f"  {spinner()}  ", style="#00f5d4")
            text.append("hashing local files", style="bold")
            text.append(f"   {self.checked}/{self._total}\n", style="#9bff3c")
        else:
            bad = len(self.missing) + len(self.corrupt)
            text.append("  ✔  " if not bad else "  ✖  ",
                        style="bold #9bff3c" if not bad else "bold #ff5f7e")
            text.append("verification complete\n", style="bold")
        text.append("  ")
        text.append(f"ok {self.ok}", style="#9bff3c")
        text.append(f"   missing {len(self.missing)}",
                    style="#ff5f7e" if self.missing else "#3f7a6b")
        text.append(f"   corrupt {len(self.corrupt)}",
                    style="#ff5f7e" if self.corrupt else "#3f7a6b")
        text.append(f"   no checksum {self.unverifiable}", style="#3f7a6b")
        self.query_one("#status-line", Static).update(text)
        self.query_one("#overall", ProgressBar).update(
            total=max(self._total, 1), progress=self.checked
        )

    def _finish(self) -> None:
        log = self.query_one("#log", RichLog)
        log.write("")
        bad = self.missing + self.corrupt
        if bad:
            log.write(
                f"{len(bad)} files need re-downloading "
                f"({human(sum(n.size for n in bad))}). Press f to requeue them."
            )
        else:
            log.write("local mirror matches Drive")
        self.notify("verify done", severity="error" if bad else "information")

    def action_forget(self) -> None:
        if self.running:
            self.notify("still verifying", severity="warning")
            return
        bad = self.missing + self.corrupt
        if not bad:
            self.notify("nothing to requeue")
            return
        for node in bad:
            self.app.dlstate.forget(node.key)
        self.app.dlstate.save()
        self.query_one("#log", RichLog).write(
            f"{len(bad)} files marked missing - run a download to fetch them"
        )
        self.missing = []
        self.corrupt = []
        self.notify(f"{len(bad)} files requeued")

    def action_cancel(self) -> None:
        self.cancelled = True

    def on_unmount(self) -> None:
        self.cancelled = True

    def action_back(self) -> None:
        self.cancelled = True
        self.app.pop_screen()
