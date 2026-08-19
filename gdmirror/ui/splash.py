"""Animated 3D wordmark shown on launch."""

from __future__ import annotations

import shutil
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from .. import banner
from ..auth import has_token
from ..config import CREDENTIALS_FILE
from ..util import existing_parent, human

WIPE = 0.75      # seconds of column wipe-in
SWEEP = 1.35     # sweep band finishes here
HOLD = 4.2       # auto-advance
TAGLINE = "// google drive folder mirror · resumable · checksum verified"


class SplashScreen(Screen):
    BINDINGS = [
        Binding("enter", "skip", "continue"),
        Binding("space", "skip", "continue", show=False),
        Binding("escape", "skip", "skip", show=False),
        Binding("q", "app.quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-wrap"):
            yield Static(id="banner")
            yield Static(id="tagline")
            yield Static(id="boot")

    def on_mount(self) -> None:
        self._t0 = time.monotonic()
        self._done = False
        self._checks = self._boot_checks()
        self.set_interval(1 / 25, self._tick)

    # -- content ----------------------------------------------------------

    def _boot_checks(self) -> list[tuple[str, str, str]]:
        """(label, value, style) rows revealed one by one under the wordmark.

        Reads the running app's configuration rather than reloading from disk, so
        command-line overrides show the values actually in use.
        """
        cfg = self.app.cfg
        free = shutil.disk_usage(existing_parent(cfg.dest_path)).free
        cred_ok = CREDENTIALS_FILE.exists()
        return [
            (
                "oauth client",
                "loaded" if cred_ok else "MISSING credential.json",
                "#9bff3c" if cred_ok else "#ff5f7e",
            ),
            (
                "token cache",
                "present" if has_token() else "none - will ask for consent",
                "#9bff3c" if has_token() else "#ffd166",
            ),
            (
                "target folder",
                cfg.folder_id or "not set - open Settings",
                "#4fd6b0" if cfg.folder_id else "#ffd166",
            ),
            ("destination", str(cfg.dest_path), "#4fd6b0"),
            (
                "free space",
                human(free),
                "#9bff3c" if free > 2 * 1024**3 else "#ff5f7e",
            ),
        ]

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._t0

        if elapsed < WIPE:
            art = banner.render(reveal=elapsed / WIPE, sweep=None)
        elif elapsed < SWEEP:
            art = banner.render(
                reveal=1.0, sweep=(elapsed - WIPE) / (SWEEP - WIPE)
            )
        else:
            # slow breathing pulse once the sweep has passed
            phase = (elapsed - SWEEP) % 2.4 / 2.4
            dim = 0.18 * (1 - abs(phase * 2 - 1))
            art = banner.render(reveal=1.0, sweep=None, dim=dim)
        self.query_one("#banner", Static).update(art)

        if elapsed > WIPE * 0.8:
            shown = int((elapsed - WIPE * 0.8) / 0.02)
            tag = Text(TAGLINE[:shown], style="#4fd6b0")
            if shown < len(TAGLINE):
                tag.append("▌", style="#9bff3c")
            self.query_one("#tagline", Static).update(tag)

        if elapsed > SWEEP:
            self.query_one("#boot", Static).update(self._boot_text(elapsed - SWEEP))

        if elapsed > HOLD and not self._done:
            self.action_skip()

    def _boot_text(self, since: float) -> Text:
        text = Text()
        text.append_text(banner.rule(52, phase=min(since / 1.2, 1.0)))
        text.append("\n")
        for i, (label, value, style) in enumerate(self._checks):
            if since < 0.12 * i:
                break
            text.append("  ▸ ", style="#0d6e5f")
            text.append(f"{label:<14}", style="#3f7a6b")
            text.append(f"{value}\n", style=style)
        if since > 0.12 * len(self._checks) + 0.2:
            text.append("\n  press ", style="#3f7a6b")
            text.append("ENTER", style="bold #00f5d4")
            text.append(" to continue", style="#3f7a6b")
        return text

    def action_skip(self) -> None:
        if self._done:
            return
        self._done = True
        self.app.open_menu()
