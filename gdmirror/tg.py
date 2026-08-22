"""Telegram client wrapper.

Telethon is asyncio; the rest of the app is threads. This owns one event loop
on a private thread and exposes plain blocking methods on top of it, so screens
and the pipeline never touch asyncio directly.

Secrets policy: the phone number, login code and 2FA password are passed
straight to Telethon and never stored, echoed or logged. Only the session file
Telethon writes persists, and it is chmod 600.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Callable

from .tgconfig import SESSION_FILE

VIDEO_EXT = {".mp4", ".m4v", ".mov"}
# Telethon's own ceiling for a non-premium account.
MAX_UPLOAD = 2 * 1024**3


class TelegramError(RuntimeError):
    pass


class PasswordNeeded(TelegramError):
    """2FA is on; the sign-in needs the account password next."""


class TG:
    def __init__(self, api_id: int, api_hash: str, session: Path = SESSION_FILE) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session = session
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._phone_hash: str | None = None
        self._phone: str | None = None

    # -- loop plumbing ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=runner, daemon=True, name="telethon")
        self._thread.start()
        ready.wait(10)
        self._call(self._connect())

    def _call(self, coro, timeout: float | None = None):
        if self._loop is None:
            raise TelegramError("telegram loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    async def _connect(self) -> None:
        from telethon import TelegramClient

        if self._client is None:
            self._client = TelegramClient(
                str(self.session), self.api_id, self.api_hash,
                device_model="gdmirror", app_version="0.1.0",
            )
        if not self._client.is_connected():
            await self._client.connect()
        if self.session.exists():
            self.session.chmod(0o600)

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            if self._client is not None:
                self._call(self._client.disconnect(), timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
        self._thread = None

    # -- sign in ----------------------------------------------------------

    def is_authorized(self) -> bool:
        try:
            return bool(self._call(self._client.is_user_authorized(), timeout=30))
        except Exception:
            return False

    def send_code(self, phone: str) -> None:
        """Ask Telegram to send the login code to `phone`."""
        async def go():
            sent = await self._client.send_code_request(phone)
            return sent.phone_code_hash

        try:
            self._phone_hash = self._call(go(), timeout=60)
            self._phone = phone
        except Exception as exc:
            raise TelegramError(_clean(exc)) from exc

    def sign_in_code(self, code: str) -> None:
        """Complete sign-in with the emailed/SMS code. May raise PasswordNeeded."""
        from telethon.errors import SessionPasswordNeededError

        async def go():
            await self._client.sign_in(
                phone=self._phone, code=code, phone_code_hash=self._phone_hash
            )

        try:
            self._call(go(), timeout=60)
        except SessionPasswordNeededError as exc:
            raise PasswordNeeded("two-factor password required") from exc
        except Exception as exc:
            raise TelegramError(_clean(exc)) from exc
        self._harden_session()

    def sign_in_password(self, password: str) -> None:
        try:
            self._call(self._client.sign_in(password=password), timeout=60)
        except Exception as exc:
            raise TelegramError(_clean(exc)) from exc
        finally:
            del password
        self._harden_session()

    def _harden_session(self) -> None:
        if self.session.exists():
            self.session.chmod(0o600)

    def me(self) -> str:
        try:
            user = self._call(self._client.get_me(), timeout=30)
        except Exception:
            return "?"
        name = " ".join(filter(None, [user.first_name, user.last_name])) or "account"
        return f"{name} (@{user.username})" if user.username else name

    # -- chats ------------------------------------------------------------

    def list_chats(self, limit: int = 200) -> list[dict]:
        """Groups and channels the account can post files into."""
        async def go():
            out = []
            async for dialog in self._client.iter_dialogs(limit=limit):
                entity = dialog.entity
                if not (dialog.is_channel or dialog.is_group):
                    continue
                if getattr(entity, "left", False):
                    continue
                broadcast = getattr(entity, "broadcast", False)
                if broadcast and not (
                    getattr(entity, "creator", False)
                    or getattr(entity, "admin_rights", None)
                ):
                    continue  # read-only channel
                out.append(
                    {
                        "id": dialog.id,
                        "title": dialog.title or str(dialog.id),
                        "kind": "channel" if broadcast else "group",
                        "members": getattr(entity, "participants_count", None),
                    }
                )
            return out

        try:
            return self._call(go(), timeout=120)
        except Exception as exc:
            raise TelegramError(_clean(exc)) from exc

    def create_channel(self, title: str, about: str = "") -> dict:
        """Create a private broadcast channel and return its dialog record."""
        from telethon.tl.functions.channels import CreateChannelRequest

        async def go():
            result = await self._client(
                CreateChannelRequest(title=title, about=about, megagroup=False)
            )
            channel = result.chats[0]
            return {
                "id": int(f"-100{channel.id}"),
                "title": channel.title,
                "kind": "channel",
                "members": None,
            }

        try:
            return self._call(go(), timeout=120)
        except Exception as exc:
            raise TelegramError(_clean(exc)) from exc

    # -- upload -----------------------------------------------------------

    def upload(
        self,
        path: Path,
        chat_id: int,
        caption: str,
        progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
        on_flood: Callable[[int], None] | None = None,
        retries: int = 4,
    ) -> dict:
        """Send one file. Returns {'msg_id', 'size', 'link'}. Blocks."""
        from telethon.errors import FloodWaitError

        size = path.stat().st_size
        if size > MAX_UPLOAD:
            raise TelegramError(
                f"{path.name} is {size} bytes, over Telegram's 2 GB per-file limit"
            )

        streaming = path.suffix.lower() in VIDEO_EXT

        async def go():
            return await self._client.send_file(
                chat_id,
                str(path),
                caption=caption,
                force_document=not streaming,
                supports_streaming=streaming,
                progress_callback=progress,
            )

        attempt = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise TelegramError("cancelled")
            try:
                message = self._call(go())
                break
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 60)) + 2
                if on_flood:
                    on_flood(wait)
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    if cancel is not None and cancel.is_set():
                        raise TelegramError("cancelled")
                    time.sleep(0.5)
            except Exception as exc:
                attempt += 1
                if attempt > retries:
                    raise TelegramError(_clean(exc)) from exc
                time.sleep(min(30, 2**attempt))

        remote = _media_size(message)
        if remote is not None and remote != size:
            raise TelegramError(
                f"size mismatch after upload: local {size}, telegram {remote}"
            )
        return {
            "msg_id": message.id,
            "size": remote if remote is not None else size,
            "link": _link(chat_id, message.id),
        }

    def pin(self, chat_id: int, msg_id: int) -> None:
        """Pin a message. Raises TelegramError on failure; the index root is the
        channel's entry point, so a caller needs to know if it did not pin."""
        async def go():
            return await self._client.pin_message(chat_id, msg_id, notify=False)

        self._retry(go)

    def unpin_all(self, chat_id: int) -> None:
        """Remove every existing pin. Best-effort: used to clear a previous index
        (or the old .md pin) so exactly one parent post stays pinned."""
        async def go():
            return await self._client.unpin_message(chat_id)

        try:
            self._retry(go)
        except TelegramError:
            pass

    # -- text messages (index posts) --------------------------------------

    def _retry(self, make_coro, retries: int = 4):
        """Run an API coroutine, waiting out FLOOD_WAIT and backing off on the rest."""
        from telethon.errors import FloodWaitError

        attempt = 0
        while True:
            try:
                return self._call(make_coro())
            except FloodWaitError as exc:
                time.sleep(int(getattr(exc, "seconds", 60)) + 2)
            except Exception as exc:
                attempt += 1
                if attempt > retries:
                    raise TelegramError(_clean(exc)) from exc
                time.sleep(min(30, 2**attempt))

    def send_html(self, chat_id: int, text: str) -> int:
        """Send an HTML-formatted message. Returns its id. Link previews off, so
        the many in-channel links do not each render a preview card."""
        async def go():
            return await self._client.send_message(
                chat_id, text, parse_mode="html", link_preview=False
            )

        return self._retry(go).id

    def edit_html(self, chat_id: int, msg_id: int, text: str) -> None:
        async def go():
            return await self._client.edit_message(
                chat_id, msg_id, text, parse_mode="html", link_preview=False
            )

        try:
            self._retry(go)
        except TelegramError as exc:
            # "message not modified" is not worth failing a run over
            if "not modified" not in str(exc).lower():
                raise

    def delete_messages(self, chat_id: int, msg_ids: list[int]) -> None:
        if not msg_ids:
            return

        async def go():
            return await self._client.delete_messages(chat_id, msg_ids)

        try:
            self._retry(go)
        except TelegramError:
            pass  # deleting a stale index is best-effort


def _media_size(message) -> int | None:
    document = getattr(message, "document", None)
    if document is not None:
        return getattr(document, "size", None)
    media = getattr(message, "media", None)
    document = getattr(media, "document", None) if media else None
    return getattr(document, "size", None) if document else None


def _link(chat_id: int, msg_id: int) -> str:
    internal = str(chat_id)
    if internal.startswith("-100"):
        internal = internal[4:]
        return f"https://t.me/c/{internal}/{msg_id}"
    return f"chat {chat_id} message {msg_id}"


def _clean(exc: Exception) -> str:
    text = str(exc)
    if "PHONE_CODE_INVALID" in text:
        return "wrong login code"
    if "PHONE_CODE_EXPIRED" in text:
        return "login code expired, request a new one"
    if "PASSWORD_HASH_INVALID" in text:
        return "wrong two-factor password"
    if "PHONE_NUMBER_INVALID" in text:
        return "phone number not accepted, use +country format"
    if "CHAT_WRITE_FORBIDDEN" in text or "CHAT_ADMIN_REQUIRED" in text:
        return "this account cannot post in that chat"
    return f"{type(exc).__name__}: {text}"
