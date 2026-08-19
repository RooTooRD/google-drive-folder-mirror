"""Sign-in screen: runs the loopback OAuth flow without leaving the TUI."""

from __future__ import annotations

import threading
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static

from ..auth import AuthError, run_flow
from .common import pulse_bar, spinner


class AuthScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("c", "cancel", "cancel"),
        Binding("o", "reopen", "reopen browser", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-line")
        yield RichLog(id="log", markup=False, max_lines=200, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._cancel = threading.Event()
        self._url: str | None = None
        self._phase = "waiting"
        self._t0 = time.monotonic()
        self.set_interval(1 / 15, self._pulse)
        self._log("opening your browser for Google consent...")
        self._log("grant read-only Drive access, then come back here.")
        self.run_auth()

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _pulse(self) -> None:
        text = Text()
        if self._phase == "waiting":
            text.append(f"  {spinner()}  ", style="#00f5d4")
            text.append("waiting for consent", style="bold")
            text.append(f"   {int(time.monotonic() - self._t0)}s\n", style="#3f7a6b")
            text.append("  ")
            text.append_text(pulse_bar(width=40))
        elif self._phase == "ok":
            text.append("  ✔  authenticated", style="bold #9bff3c")
        else:
            text.append("  ✖  ", style="bold #ff5f7e")
            text.append("sign-in failed - press escape to go back", style="#ff5f7e")
        self.query_one("#status-line", Static).update(text)

    # -- worker -----------------------------------------------------------

    def run_auth(self) -> None:
        self.run_worker(self._auth_worker, thread=True, exclusive=True)

    def _auth_worker(self) -> None:
        call = self.app.call_from_thread

        def on_url(url: str) -> None:
            self._url = url
            call(self._log, "")
            call(self._log, "if the browser did not open, paste this URL:")
            call(self._log, url)
            call(self._log, "")

        try:
            creds = run_flow(on_url=on_url, cancel=self._cancel)
        except AuthError as exc:
            call(self._fail, str(exc))
            return
        except Exception as exc:
            call(self._fail, f"{type(exc).__name__}: {exc}")
            return
        call(self._succeed, creds)

    def _succeed(self, creds) -> None:
        self._phase = "ok"
        self.app.creds = creds
        self._log("token saved to token.json (mode 600)")
        self.notify("authenticated")
        self.set_timer(0.8, self.action_back)

    def _fail(self, message: str) -> None:
        self._phase = "error"
        self._log("")
        self._log(f"ERROR: {message}")

    # -- actions ----------------------------------------------------------

    def action_reopen(self) -> None:
        if not self._url:
            return
        import webbrowser

        webbrowser.open(self._url, new=1, autoraise=True)

    def action_cancel(self) -> None:
        self._cancel.set()
        self._log("cancelled")

    def action_back(self) -> None:
        self._cancel.set()
        if self.is_attached:
            self.app.pop_screen()

    def on_unmount(self) -> None:
        self._cancel.set()
