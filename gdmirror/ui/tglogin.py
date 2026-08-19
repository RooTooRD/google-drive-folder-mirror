"""Telegram setup: API credentials, sign-in, and choosing the target channel.

The phone number, login code and 2FA password are typed here, handed straight
to Telethon, and never stored or written to the log.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ..tg import TG, PasswordNeeded, TelegramError
from ..tgconfig import TG_CONFIG_FILE
from .common import spinner


class TelegramSetupScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("ctrl+r", "restart", "start over", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-line")
        yield Vertical(id="settings-form")
        yield RichLog(id="log", markup=False, max_lines=200, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._busy = ""
        self._chats: list[dict] = []
        self.set_interval(1 / 12, self._pulse)
        cfg = self.app.tgcfg
        if not cfg.configured:
            self.show_creds()
        else:
            self._connect()

    # -- chrome -----------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _pulse(self) -> None:
        text = Text()
        if self._busy:
            text.append(f"  {spinner()}  ", style="#00f5d4")
            text.append(self._busy, style="bold")
        else:
            cfg = self.app.tgcfg
            text.append("  telegram  ", style="bold #00f5d4")
            text.append("api " + ("set" if cfg.configured else "missing"),
                        style="#9bff3c" if cfg.configured else "#ff5f7e")
            text.append("   target " + (cfg.chat_title or "none"),
                        style="#9bff3c" if cfg.has_target else "#ffd166")
        self.query_one("#status-line", Static).update(text)

    def _body(self) -> Vertical:
        return self.query_one("#settings-form", Vertical)

    async def _swap(self, *widgets) -> None:
        body = self._body()
        await body.remove_children()
        await body.mount(*widgets)
        for widget in widgets:
            if isinstance(widget, (Input, OptionList)):
                widget.focus()
                break

    # -- phase: api credentials -------------------------------------------

    def show_creds(self) -> None:
        cfg = self.app.tgcfg
        self.run_worker(
            self._swap(
                Static("TELEGRAM API CREDENTIALS", classes="title"),
                Static(
                    "Get these from my.telegram.org > API development tools.\n"
                    f"They are saved to {TG_CONFIG_FILE.name} (mode 600), never logged.",
                    classes="muted",
                ),
                Label("api_id"),
                Input(value=str(cfg.api_id or ""), id="api_id", type="integer"),
                Label("api_hash"),
                Input(value=cfg.api_hash, id="api_hash", password=True),
                Button("Save and connect", variant="success", id="save_creds"),
            )
        )

    def _save_creds(self) -> None:
        try:
            api_id = int(self.query_one("#api_id", Input).value.strip())
        except ValueError:
            self.notify("api_id must be a number", severity="error")
            return
        api_hash = self.query_one("#api_hash", Input).value.strip()
        if not api_hash:
            self.notify("api_hash cannot be empty", severity="error")
            return
        cfg = self.app.tgcfg
        cfg.api_id, cfg.api_hash = api_id, api_hash
        cfg.save()
        self._log("credentials saved")
        self._connect()

    # -- phase: connect / sign in -----------------------------------------

    def _connect(self) -> None:
        self._busy = "connecting to telegram"
        self.run_worker(self._connect_worker, thread=True, exclusive=True)

    def _connect_worker(self) -> None:
        call = self.app.call_from_thread
        cfg = self.app.tgcfg
        try:
            if self.app.tg is None:
                self.app.tg = TG(cfg.api_id, cfg.api_hash)
            self.app.tg.start()
            authorized = self.app.tg.is_authorized()
        except Exception as exc:
            self._busy = ""
            call(self._log, f"ERROR: {type(exc).__name__}: {exc}")
            call(self.show_creds)
            return
        self._busy = ""
        self.app.tg_authorized = authorized
        if authorized:
            call(self._log, f"signed in as {self.app.tg.me()}")
            call(self.show_chats)
        else:
            call(self.show_phone)

    def show_phone(self) -> None:
        self.run_worker(
            self._swap(
                Static("SIGN IN", classes="title"),
                Static(
                    "Telegram will send a login code to this number, in the app.\n"
                    "Nothing you type here is stored or logged.",
                    classes="muted",
                ),
                Label("phone number (with country code, e.g. +213...)"),
                Input(placeholder="+...", id="phone"),
                Button("Send code", variant="success", id="send_code"),
            )
        )

    def show_code(self) -> None:
        self.run_worker(
            self._swap(
                Static("LOGIN CODE", classes="title"),
                Static("Check your Telegram app for the code.", classes="muted"),
                Label("code"),
                Input(placeholder="12345", id="code"),
                Button("Sign in", variant="success", id="sign_in"),
            )
        )

    def show_password(self) -> None:
        self.run_worker(
            self._swap(
                Static("TWO-FACTOR PASSWORD", classes="title"),
                Static(
                    "Your account has 2FA enabled. The password goes straight to "
                    "Telegram and is never written to disk by this app.",
                    classes="muted",
                ),
                Label("password"),
                Input(password=True, id="tfa"),
                Button("Sign in", variant="success", id="sign_in_pw"),
            )
        )

    # -- phase: chat picker -----------------------------------------------

    def show_chats(self) -> None:
        self._busy = "loading your channels"
        self.run_worker(self._chats_worker, thread=True, exclusive=True)

    def _chats_worker(self) -> None:
        call = self.app.call_from_thread
        try:
            chats = self.app.tg.list_chats()
        except TelegramError as exc:
            self._busy = ""
            call(self._log, f"ERROR: {exc}")
            return
        self._busy = ""
        self._chats = chats
        call(self._render_chats)

    def _render_chats(self) -> None:
        cfg = self.app.tgcfg
        options = [
            Option(
                Text("  + CREATE A NEW PRIVATE CHANNEL", style="bold #9bff3c"),
                id="__new__",
            )
        ]
        for chat in self._chats:
            prompt = Text("  ")
            current = chat["id"] == cfg.chat_id
            prompt.append("● " if current else "○ ",
                          style="#9bff3c" if current else "#3f7a6b")
            prompt.append(chat["title"][:52], style="bold")
            members = f" · {chat['members']} members" if chat["members"] else ""
            prompt.append(f"\n      {chat['kind']}{members}", style="#3f7a6b")
            options.append(Option(prompt, id=str(chat["id"])))

        listing = OptionList(*options, id="chatlist")
        self.run_worker(
            self._swap(
                Static("TARGET CHANNEL", classes="title"),
                Static(
                    "Everything uploads here, flat, in folder order. "
                    "A private channel you own is the right choice.",
                    classes="muted",
                ),
                listing,
            )
        )

    def _choose_chat(self, chat_id: int, title: str) -> None:
        cfg = self.app.tgcfg
        cfg.chat_id = chat_id
        cfg.chat_title = title
        cfg.save()
        self._log(f"target set: {title} ({chat_id})")
        self.notify(f"target: {title}")
        self._render_chats()

    def _suggested_title(self) -> str:
        tree = getattr(self.app, "drive_tree", None)
        return tree.name if tree is not None else "Drive mirror"

    def show_new_channel(self) -> None:
        self.run_worker(
            self._swap(
                Static("NEW PRIVATE CHANNEL", classes="title"),
                Static("Created private, with you as the only member.", classes="muted"),
                Label("channel title"),
                Input(value=self._suggested_title(), id="newtitle"),
                Button("Create", variant="success", id="create_channel"),
            )
        )

    # -- events -----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "save_creds": self._save_creds,
            "send_code": self._do_send_code,
            "sign_in": self._do_sign_in,
            "sign_in_pw": self._do_sign_in_pw,
            "create_channel": self._do_create,
        }
        handler = handlers.get(str(event.button.id))
        if handler:
            handler()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        mapping = {
            "api_hash": self._save_creds,
            "phone": self._do_send_code,
            "code": self._do_sign_in,
            "tfa": self._do_sign_in_pw,
            "newtitle": self._do_create,
        }
        handler = mapping.get(str(event.input.id))
        if handler:
            handler()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = str(event.option.id)
        if chosen == "__new__":
            self.show_new_channel()
            return
        title = next(
            (c["title"] for c in self._chats if str(c["id"]) == chosen), chosen
        )
        self._choose_chat(int(chosen), title)

    # -- network actions --------------------------------------------------

    def _do_send_code(self) -> None:
        phone = self.query_one("#phone", Input).value.strip()
        if not phone:
            return
        self._busy = "requesting login code"

        def work() -> None:
            call = self.app.call_from_thread
            try:
                self.app.tg.send_code(phone)
            except TelegramError as exc:
                self._busy = ""
                call(self._log, f"ERROR: {exc}")
                return
            self._busy = ""
            call(self._log, "code sent")
            call(self.show_code)

        self.run_worker(work, thread=True, exclusive=True)

    def _do_sign_in(self) -> None:
        code = self.query_one("#code", Input).value.strip()
        if not code:
            return
        self._busy = "signing in"

        def work() -> None:
            call = self.app.call_from_thread
            try:
                self.app.tg.sign_in_code(code)
            except PasswordNeeded:
                self._busy = ""
                call(self.show_password)
                return
            except TelegramError as exc:
                self._busy = ""
                call(self._log, f"ERROR: {exc}")
                return
            self._busy = ""
            self.app.tg_authorized = True
            call(self._log, f"signed in as {self.app.tg.me()}")
            call(self.show_chats)

        self.run_worker(work, thread=True, exclusive=True)

    def _do_sign_in_pw(self) -> None:
        password = self.query_one("#tfa", Input).value
        if not password:
            return
        self._busy = "checking password"

        def work() -> None:
            call = self.app.call_from_thread
            try:
                self.app.tg.sign_in_password(password)
            except TelegramError as exc:
                self._busy = ""
                call(self._log, f"ERROR: {exc}")
                return
            self._busy = ""
            self.app.tg_authorized = True
            call(self._log, f"signed in as {self.app.tg.me()}")
            call(self.show_chats)

        self.run_worker(work, thread=True, exclusive=True)

    def _do_create(self) -> None:
        title = self.query_one("#newtitle", Input).value.strip()
        if not title:
            return
        self._busy = "creating channel"

        def work() -> None:
            call = self.app.call_from_thread
            try:
                chat = self.app.tg.create_channel(title, "mirrored from Google Drive")
            except TelegramError as exc:
                self._busy = ""
                call(self._log, f"ERROR: {exc}")
                return
            self._busy = ""
            self._chats.insert(0, chat)
            call(self._choose_chat, chat["id"], chat["title"])

        self.run_worker(work, thread=True, exclusive=True)

    # -- exit -------------------------------------------------------------

    def action_restart(self) -> None:
        self.show_creds()

    def action_back(self) -> None:
        self.app.pop_screen()
