"""The application object: shared state, theme, and screen wiring."""

from __future__ import annotations

from textual.app import App

from ..drive import Node
from ..settings import Settings
from ..state import State
from ..tgconfig import TgConfig

CYBER_CSS = """
Screen {
    background: #04100d;
    color: #b8ffe4;
}
Header { background: #072019; color: #00f5d4; }
Footer { background: #072019; color: #4fd6b0; }
Footer > .footer-key--key { color: #04100d; background: #00f5d4; }

.panel {
    border: round #0d6e5f;
    background: #06170f;
    padding: 0 1;
}
.panel-accent { border: round #9bff3c; }
.title { color: #00f5d4; text-style: bold; }
.muted { color: #3f7a6b; }
.danger { color: #ff5f7e; text-style: bold; }

#splash-wrap { align: center middle; height: 1fr; }
#banner { width: auto; height: auto; }
#tagline { width: auto; height: auto; padding-top: 1; }
#boot { width: auto; height: auto; padding-top: 1; }

#menu-head { height: auto; padding: 1 2 0 2; }
#menu-body { height: 1fr; }
#menu-list {
    width: 2fr;
    border: round #0d6e5f;
    background: #06170f;
    padding: 1 1;
}
#menu-list > .option-list--option-highlighted {
    background: #00f5d4;
    color: #04100d;
    text-style: bold;
}
#menu-status { width: 42; border: round #9bff3c; background: #06170f; padding: 1 2; }

#status-line { height: auto; padding: 1 2; background: #072019; }
#log { height: 1fr; border: round #0d6e5f; background: #050f0c; padding: 0 1; }

#tree { width: 2fr; border: round #0d6e5f; background: #06170f; padding: 0 1; }
#tree > .tree--cursor { background: #00f5d4; color: #04100d; }
#info { width: 38; border: round #9bff3c; background: #06170f; padding: 1 2; }

#overall { width: 100%; padding: 0 2; }
Bar > .bar--bar { color: #00f5d4; }
Bar > .bar--complete { color: #9bff3c; }
#active { height: 11; border: round #0d6e5f; background: #06170f; padding: 1 2; }

#dialog {
    width: 66; height: auto; padding: 1 2;
    background: #06170f; border: thick #ff5f7e;
}
#dialog-title { text-style: bold; color: #ff5f7e; padding-bottom: 1; }
#dialog-buttons { height: auto; padding-top: 1; align-horizontal: right; }
#dialog-buttons Button { margin-left: 2; }
ConfirmScreen { align: center middle; }

#settings-form { padding: 1 2; height: auto; }
#settings-form Label { padding-top: 1; color: #4fd6b0; }
Input { background: #06170f; border: tall #0d6e5f; }
Input:focus { border: tall #00f5d4; }
Button { background: #0d6e5f; color: #04100d; }
Button:hover { background: #00f5d4; }
"""


class MirrorApp(App):
    TITLE = "gdmirror"
    SUB_TITLE = "google drive folder mirror"
    CSS = CYBER_CSS

    def __init__(self, cfg: Settings | None = None, splash: bool = True) -> None:
        super().__init__()
        self.cfg = cfg or Settings.load()
        self.tgcfg = TgConfig.load()
        self.dlstate = State()
        self.creds = None
        self.tg = None
        self.tg_authorized = False  # cached; is_authorized() is a network call
        self.drive_tree: Node | None = None
        self.show_splash = splash

    def on_mount(self) -> None:
        from .splash import SplashScreen
        from .menu import MenuScreen

        # Reuse a cached token silently so the menu opens already authenticated.
        from ..auth import load_cached

        self.creds = load_cached()

        from ..drive import load_tree

        self.drive_tree = load_tree(self.cfg.folder_id)

        self.push_screen(SplashScreen() if self.show_splash else MenuScreen())

    def open_menu(self) -> None:
        from .menu import MenuScreen

        self.switch_screen(MenuScreen())

    def telegram_ready(self) -> bool:
        return self.tg is not None and self.tg_authorized and self.tgcfg.has_target

    def on_unmount(self) -> None:
        if self.tg is not None:
            self.tg.close()
