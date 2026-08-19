"""Main menu: every action in the app is reachable from here."""

from __future__ import annotations

import shutil
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from .. import banner
from ..util import existing_parent, human

ITEMS: list[tuple[str, str, str]] = [
    ("auth", "AUTHENTICATE", "sign in to Google, cache a read-only token"),
    ("scan", "SCAN DRIVE FOLDER", "walk the folder, measure it, cache the tree"),
    ("browse", "BROWSE & SELECT", "pick folders that fit, then download"),
    ("all", "DOWNLOAD EVERYTHING", "queue every file in the tree"),
    ("verify", "VERIFY LOCAL MIRROR", "re-check downloaded files against Drive md5"),
    ("telegram", "TELEGRAM SETUP", "api credentials, sign in, pick the channel"),
    ("pipe", "MIRROR TO TELEGRAM", "download → upload → delete, inside a disk budget"),
    ("settings", "SETTINGS", "folder id, destination, workers, buffer"),
    ("quit", "QUIT", "leave"),
]


class MenuScreen(Screen):
    BINDINGS = [
        Binding("q", "app.quit", "quit"),
        Binding("a", "jump('auth')", "auth", show=False),
        Binding("s", "jump('scan')", "scan", show=False),
        Binding("b", "jump('browse')", "browse", show=False),
        Binding("v", "jump('verify')", "verify", show=False),
        Binding("t", "jump('telegram')", "telegram", show=False),
        Binding("p", "jump('pipe')", "pipeline", show=False),
        Binding("comma", "jump('settings')", "settings", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="menu-head")
        with Horizontal(id="menu-body"):
            yield OptionList(id="menu-list")
            yield Static(id="menu-status", classes="panel-accent")
        yield Footer()

    def on_mount(self) -> None:
        self._t0 = time.monotonic()
        self._build_options()
        self.query_one("#menu-list", OptionList).focus()
        self.set_interval(1 / 15, self._pulse)
        self._refresh_status()

    def on_screen_resume(self) -> None:
        self._build_options()
        self._refresh_status()

    # -- widgets ----------------------------------------------------------

    def _build_options(self) -> None:
        app = self.app
        options = []
        for key, label, hint in ITEMS:
            if key == "auth" and app.creds is not None:
                label, hint = "RE-AUTHENTICATE", "token is valid; sign in again anyway"
            ready = self._ready(key)
            prompt = Text()
            prompt.append("  ")
            prompt.append("▪ " if ready else "▫ ", style="#9bff3c" if ready else "#3f7a6b")
            prompt.append(f"{label}\n", style="bold" if ready else "#3f7a6b")
            prompt.append(f"     {hint}", style="#3f7a6b")
            options.append(Option(prompt, id=key))

        listing = self.query_one("#menu-list", OptionList)
        highlighted = listing.highlighted
        listing.clear_options()
        listing.add_options(options)
        if highlighted is not None and highlighted < len(options):
            listing.highlighted = highlighted

    def _ready(self, key: str) -> bool:
        app = self.app
        if key in {"auth", "settings", "quit", "telegram"}:
            return True
        if key == "scan":
            return app.creds is not None and bool(app.cfg.folder_id)
        if key == "pipe":
            return (
                app.creds is not None
                and app.drive_tree is not None
                and app.telegram_ready()
            )
        return app.drive_tree is not None and app.creds is not None

    def _pulse(self) -> None:
        phase = (time.monotonic() - self._t0) % 3.0 / 3.0
        head = Text()
        head.append("drive", style="bold #00f5d4")
        head.append("mirror", style="bold #9bff3c")
        head.append("  ·  ", style="#0d6e5f")
        head.append("control panel", style="#3f7a6b")
        head.append("\n")
        head.append_text(banner.rule(70, phase=phase))
        self.query_one("#menu-head", Static).update(head)

    def _refresh_status(self) -> None:
        app = self.app
        cfg = app.cfg
        free = shutil.disk_usage(existing_parent(cfg.dest_path)).free

        text = Text()
        text.append("SESSION\n", style="bold #00f5d4")
        ok = app.creds is not None
        text.append("  auth      ")
        text.append("authenticated\n" if ok else "not signed in\n",
                    style="#9bff3c" if ok else "#ff5f7e")

        text.append("  folder    ")
        if cfg.folder_id:
            text.append(f"{cfg.folder_id[:22]}…\n", style="#4fd6b0")
        else:
            text.append("not set - see Settings\n", style="#ffd166")

        text.append("\nTREE\n", style="bold #00f5d4")
        if app.drive_tree is None:
            text.append("  not scanned yet\n", style="#ffd166")
        else:
            files = list(app.drive_tree.walk_files())
            total = sum(f.size for f in files)
            text.append(f"  root      {app.drive_tree.name[:24]}\n", style="#4fd6b0")
            text.append(f"  files     {len(files)}\n")
            text.append(f"  size      {human(total)}\n")
            fits = total < free - 2 * 1024**3
            text.append("  fits      ")
            text.append("yes\n" if fits else "NO - select a subset\n",
                        style="#9bff3c" if fits else "#ff5f7e")

        text.append("\nTELEGRAM\n", style="bold #00f5d4")
        tgcfg = app.tgcfg
        if not tgcfg.configured:
            text.append("  no api credentials\n", style="#ffd166")
        elif not app.tg_authorized:
            text.append("  not signed in\n", style="#ffd166")
        else:
            text.append("  signed in\n", style="#9bff3c")
        if tgcfg.has_target:
            text.append(f"  → {tgcfg.chat_title[:24]}\n", style="#4fd6b0")
        else:
            text.append("  no target channel\n", style="#ffd166")
        text.append(f"  sent      {app.dlstate.uploaded_count} files"
                    f" · {human(app.dlstate.uploaded_bytes)}\n", style="#4fd6b0")
        text.append(f"  buffer    {tgcfg.buffer_gb:g} GB\n", style="#4fd6b0")

        text.append("\nDISK\n", style="bold #00f5d4")
        text.append(f"  dest      {str(cfg.dest_path)[-26:]}\n", style="#4fd6b0")
        text.append(f"  free      {human(free)}\n",
                    style="#9bff3c" if free > 2 * 1024**3 else "#ff5f7e")
        text.append(f"  done      {len(app.dlstate)} files\n", style="#4fd6b0")
        text.append(f"  workers   {cfg.workers}\n", style="#4fd6b0")

        self.query_one("#menu-status", Static).update(text)

    # -- actions ----------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._dispatch(str(event.option.id))

    def action_jump(self, key: str) -> None:
        self._dispatch(key)

    def _dispatch(self, key: str) -> None:
        app = self.app

        if key == "quit":
            app.exit()
            return

        if key == "auth":
            from .authscr import AuthScreen

            app.push_screen(AuthScreen())
            return

        if key == "settings":
            from .settingsscr import SettingsScreen

            app.push_screen(SettingsScreen())
            return

        if key == "telegram":
            from .tglogin import TelegramSetupScreen

            app.push_screen(TelegramSetupScreen())
            return

        if app.creds is None:
            self.notify("sign in first", severity="warning")
            return

        if key == "scan":
            if not app.cfg.folder_id:
                self.notify(
                    "set the Drive folder id in Settings first", severity="warning"
                )
                return
            from .scan import ScanScreen

            app.push_screen(ScanScreen())
            return

        if app.drive_tree is None:
            self.notify("scan the folder first", severity="warning")
            return

        if key == "browse":
            from .browse import BrowseScreen

            app.push_screen(BrowseScreen(app.drive_tree))
        elif key == "all":
            from .download import DownloadScreen

            files = list(app.drive_tree.walk_files())
            if not files:
                self.notify("folder is empty", severity="warning")
                return
            app.push_screen(DownloadScreen(files))
        elif key == "verify":
            from .verify import VerifyScreen

            app.push_screen(VerifyScreen(app.drive_tree))
        elif key == "pipe":
            if not app.telegram_ready():
                self.notify("finish telegram setup first", severity="warning")
                return
            from .pipelinescr import PipelineScreen

            files = [
                f for f in app.drive_tree.walk_files()
                if not app.dlstate.is_uploaded(f.key)
            ]
            if not files:
                self.notify("everything is already in telegram")
                return
            app.push_screen(PipelineScreen(files))
