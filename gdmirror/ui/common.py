"""Shared widgets and helpers for the screens."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PULSE = "▁▂▃▄▅▆▇█▇▆▅▄▃▂"


def spinner(t: float | None = None) -> str:
    t = time.monotonic() if t is None else t
    return SPINNER[int(t * 12) % len(SPINNER)]


def pulse_bar(t: float | None = None, width: int = 24) -> Text:
    t = time.monotonic() if t is None else t
    text = Text()
    for i in range(width):
        text.append(PULSE[int(t * 14 + i) % len(PULSE)], style="#0d6e5f")
    return text


class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, title: str, body: str, confirm_label: str = "Continue") -> None:
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dialog-title")
            yield Static(self.body_text, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", variant="primary", id="cancel")
                yield Button(self.confirm_label, variant="error", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
