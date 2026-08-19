"""Settings form: folder id, destination, workers, checksum verification."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static


class SettingsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("ctrl+s", "save", "save"),
    ]

    def compose(self) -> ComposeResult:
        cfg = self.app.cfg
        yield Header()
        with Vertical(id="settings-form"):
            yield Static("SETTINGS", classes="title")
            yield Label("Drive folder id")
            yield Input(value=cfg.folder_id, id="folder")
            yield Label("Destination directory")
            yield Input(value=str(cfg.dest_path), id="dest")
            yield Label("Parallel workers (1-16)")
            yield Input(value=str(cfg.workers), id="workers", type="integer")
            yield Checkbox("Verify md5 after each file", cfg.verify_md5, id="verify")

            tgcfg = self.app.tgcfg
            yield Static("TELEGRAM PIPELINE", classes="title")
            yield Label("Disk buffer in GB (files held locally at once)")
            yield Input(value=f"{tgcfg.buffer_gb:g}", id="buffer")
            yield Label("Pause between uploads in seconds (flood protection)")
            yield Input(value=f"{tgcfg.throttle_seconds:g}", id="throttle")
            yield Checkbox(
                "Delete local copy after Telegram confirms the upload",
                tgcfg.purge_after_upload,
                id="purge",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Back", id="back")
                yield Button("Save", variant="success", id="save")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#folder", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_back()

    def action_save(self) -> None:
        cfg = self.app.cfg
        folder = self.query_one("#folder", Input).value.strip()
        dest = self.query_one("#dest", Input).value.strip()
        raw_workers = self.query_one("#workers", Input).value.strip()

        if not folder:
            self.notify("folder id cannot be empty", severity="error")
            return
        if not dest:
            self.notify("destination cannot be empty", severity="error")
            return
        try:
            workers = max(1, min(16, int(raw_workers)))
        except ValueError:
            self.notify("workers must be a number", severity="error")
            return

        try:
            buffer_gb = max(0.5, float(self.query_one("#buffer", Input).value.strip()))
            throttle = max(0.0, float(self.query_one("#throttle", Input).value.strip()))
        except ValueError:
            self.notify("buffer and throttle must be numbers", severity="error")
            return

        tgcfg = self.app.tgcfg
        tgcfg.buffer_gb = buffer_gb
        tgcfg.throttle_seconds = throttle
        tgcfg.purge_after_upload = self.query_one("#purge", Checkbox).value
        tgcfg.save()

        changed_folder = folder != cfg.folder_id
        cfg.folder_id = folder
        cfg.dest = dest
        cfg.workers = workers
        cfg.verify_md5 = self.query_one("#verify", Checkbox).value
        cfg.save()

        if changed_folder:
            # The cached tree belongs to the old folder.
            self.app.drive_tree = None
            self.notify("saved - folder changed, scan again")
        else:
            self.notify("saved")
        self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()
