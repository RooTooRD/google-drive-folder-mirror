"""OAuth for an installed (desktop) client, drivable from inside the TUI.

`run_flow` reimplements the useful half of InstalledAppFlow.run_local_server so
the authorization URL can be handed to the UI instead of printed to a terminal
the TUI already owns, and so a waiting flow can be cancelled.
"""

from __future__ import annotations

import threading
import wsgiref.simple_server
import wsgiref.util
from typing import Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import CREDENTIALS_FILE, SCOPES, TOKEN_FILE

SUCCESS_HTML = """<!doctype html><meta charset="utf-8">
<title>gdmirror</title>
<style>
 body{background:#06120f;color:#9bff3c;font:16px/1.6 ui-monospace,monospace;
      display:grid;place-items:center;height:100vh;margin:0}
 div{text-align:center} b{color:#00f5d4}
</style>
<div><h1>&#10003; authorized</h1>
<p><b>gdmirror</b> now has read-only access to your Drive.</p>
<p>You can close this tab and go back to the terminal.</p></div>
"""


class AuthError(RuntimeError):
    pass


def has_token() -> bool:
    return TOKEN_FILE.exists()


def _save(creds: Credentials) -> None:
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)


def load_cached() -> Credentials | None:
    """Return usable credentials from disk, refreshing if needed. No prompting."""
    if not TOKEN_FILE.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    except ValueError:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            return None
        _save(creds)
        return creds
    return None


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, *args) -> None:  # never write to the TUI's terminal
        pass


def run_flow(
    on_url: Callable[[str], None],
    cancel: threading.Event | None = None,
    open_browser: bool = True,
) -> Credentials:
    """Run the consent flow. Blocks until Google redirects back, or cancel is set."""
    import webbrowser

    if not CREDENTIALS_FILE.exists():
        raise AuthError(f"missing OAuth client file: {CREDENTIALS_FILE}")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    except ValueError as exc:
        raise AuthError(f"bad client file: {exc}") from exc

    seen: list[str] = []

    def wsgi_app(environ, start_response):
        start_response("200 OK", [("Content-type", "text/html; charset=utf-8")])
        seen.append(wsgiref.util.request_uri(environ))
        return [SUCCESS_HTML.encode("utf-8")]

    server = wsgiref.simple_server.make_server(
        "localhost", 0, wsgi_app, handler_class=_QuietHandler
    )
    server.timeout = 0.5

    try:
        flow.redirect_uri = f"http://localhost:{server.server_port}/"
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true"
        )
        on_url(auth_url)
        if open_browser:
            try:
                webbrowser.open(auth_url, new=1, autoraise=True)
            except Exception:
                pass

        while not seen:
            if cancel is not None and cancel.is_set():
                raise AuthError("cancelled")
            server.handle_request()
    finally:
        server.server_close()

    # oauthlib refuses a plain-http callback; the loopback redirect is safe.
    response_url = seen[0].replace("http://", "https://", 1)
    try:
        flow.fetch_token(authorization_response=response_url)
    except Exception as exc:
        raise AuthError(_explain(exc)) from exc

    creds = flow.credentials
    _save(creds)
    return creds


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "access_denied" in text:
        return (
            "access denied. If the app is in Testing mode, add your Google "
            "account under Audience > Test users in the Cloud console."
        )
    return f"{type(exc).__name__}: {text}"
