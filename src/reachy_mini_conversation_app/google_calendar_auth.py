"""Reusable Google Calendar OAuth (Desktop / loopback) helper.

Designed to drive consent from a UI: instead of auto-opening a browser, it
hands you the Google consent URL to show as a link. When the user clicks it and
approves, Google redirects to a tiny local listener that captures the auth code,
exchanges it for tokens, and saves them. No copy-paste, no manual code entry.

Loopback redirect means the browser and this process must be on the same
machine (true for the local headless web UI). For a remote host use a Web client
with a public redirect URI instead.

Paths come from environment variables so no secret paths live in code:

    GOOGLE_OAUTH_CLIENT_FILE   OAuth client secrets JSON (Desktop app client).
    GOOGLE_OAUTH_TOKEN_FILE    Where to store/read the user token.
                               Defaults to google_calendar_token.json under the
                               user config dir (next to other app settings).

Two ways to use it:

  * Blocking (CLI/tests):  start() -> show url -> wait_for_completion()
  * Background (web UI):   start_background() -> show url -> poll status()
"""

from __future__ import annotations
import os
import logging
import threading
import wsgiref.simple_server
from typing import Optional
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request


logger = logging.getLogger(__name__)

# Full read/write scope: create, edit, delete events and invite attendees.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

_CLIENT_FILE_ENV = "GOOGLE_OAUTH_CLIENT_FILE"
_TOKEN_FILE_ENV = "GOOGLE_OAUTH_TOKEN_FILE"
_REDIRECT_PORT_ENV = "GOOGLE_OAUTH_REDIRECT_PORT"
_DEFAULT_TOKEN = Path(__file__).with_name("google_calendar_token.json")
# Fixed loopback port so the redirect URI is stable and can be SSH-forwarded
# (the app runs on the device, consent happens in a browser on another machine).
# 0 means "let the OS pick a free port" (fine for same-machine local use).
_DEFAULT_REDIRECT_PORT = 8789


def redirect_port() -> int:
    """Return the loopback OAuth redirect port from the environment or the default."""
    raw = os.environ.get(_REDIRECT_PORT_ENV)
    if raw is None:
        return _DEFAULT_REDIRECT_PORT
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_REDIRECT_PORT


def client_file_path() -> Optional[Path]:
    """Return the OAuth client-secrets file path from the environment, if configured."""
    path = os.environ.get(_CLIENT_FILE_ENV)
    return Path(path).expanduser() if path else None


def token_file_path() -> Path:
    """Return the path where the saved OAuth token is stored."""
    return Path(os.environ.get(_TOKEN_FILE_ENV, str(_DEFAULT_TOKEN))).expanduser()


def load_saved_credentials() -> Optional[Credentials]:
    """Return saved creds (refreshing if needed), or None if none/unusable."""
    token_file = token_file_path()
    if not token_file.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except Exception as exc:
        logger.warning("Could not load saved calendar token: %s", exc)
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
            return creds
        except Exception as exc:
            logger.warning("Could not refresh calendar token: %s", exc)
            return None
    return None


class _CodeReceiver:
    """Minimal WSGI app that captures the OAuth redirect's query string once."""

    def __init__(self) -> None:
        self.query: Optional[str] = None
        self._event = threading.Event()

    def __call__(self, environ, start_response):
        self.query = environ.get("QUERY_STRING", "")
        self._event.set()
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        body = (
            "<html><body style='font-family:sans-serif'>"
            "<h2>Authorization complete.</h2>"
            "<p>You can close this tab and return to the app.</p></body></html>"
        )
        return [body.encode("utf-8")]

    def wait(self, timeout: Optional[float]) -> bool:
        return self._event.wait(timeout)


class CalendarAuth:
    """Loopback OAuth flow whose consent URL can be surfaced to a UI.

    A single instance is meant to be shared (it tracks one in-flight flow).
    """

    def __init__(self) -> None:
        """Initialize an idle auth flow with no in-flight server or credentials."""
        self._flow: Optional[Flow] = None
        self._server: Optional[wsgiref.simple_server.WSGIServer] = None
        self._receiver: Optional[_CodeReceiver] = None
        self._state = "idle"  # idle | pending | connected | error
        self._error: Optional[str] = None
        self._lock = threading.Lock()

    # --- queries -----------------------------------------------------------
    def has_valid_credentials(self) -> bool:
        """Return whether usable saved credentials are available."""
        try:
            return load_saved_credentials() is not None
        except Exception:
            return False

    def get_credentials(self) -> Credentials:
        """Return saved credentials, raising if the user has not authorized yet."""
        creds = load_saved_credentials()
        if creds is None:
            raise RuntimeError("No valid credentials. Authorize via the UI first.")
        return creds

    def status(self) -> dict:
        """State for the UI: connected / pending / error / idle (+ config hint)."""
        if self.has_valid_credentials():
            return {"state": "connected"}
        return {
            "state": self._state if self._state != "connected" else "idle",
            "error": self._error,
            "client_configured": client_file_path() is not None,
        }

    # --- flow --------------------------------------------------------------
    def start(self) -> str:
        """Start a local listener and return the Google consent URL to display."""
        client = client_file_path()
        if client is None:
            raise RuntimeError(
                f"{_CLIENT_FILE_ENV} is not set. Point it at the Desktop OAuth "
                "client secrets JSON from Google Cloud Console."
            )
        if not client.exists():
            raise RuntimeError(f"OAuth client file not found: {client}")

        with self._lock:
            self._receiver = _CodeReceiver()
            # Fixed port (env-overridable) so the redirect URI is stable and can
            # be SSH-forwarded; port 0 falls back to an OS-picked free port.
            self._server = wsgiref.simple_server.make_server(
                "localhost", redirect_port(), self._receiver
            )
            port = self._server.server_address[1]
            self._flow = Flow.from_client_secrets_file(str(client), SCOPES)
            self._flow.redirect_uri = f"http://localhost:{port}/"
            auth_url, _ = self._flow.authorization_url(
                access_type="offline",  # request a refresh token
                prompt="consent",       # ensure a refresh token is returned
            )
            self._state = "pending"
            self._error = None
        return auth_url

    def wait_for_completion(self, timeout: Optional[float] = 300) -> Credentials:
        """Block until the user approves (or timeout), then save and return creds."""
        if self._flow is None or self._receiver is None or self._server is None:
            raise RuntimeError("Call start() first.")
        try:
            # Single request: serve the redirect, then return.
            self._server.timeout = timeout
            self._server.handle_request()
            if not self._receiver.query:
                raise TimeoutError("Timed out waiting for authorization.")
            full_url = f"{self._flow.redirect_uri}?{self._receiver.query}"
            # The redirect is http://localhost (loopback) which Google permits but
            # oauthlib rejects by default ("insecure_transport"). Allow it just for
            # the loopback exchange -- run_local_server sets the same flag internally.
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
            # Google always grants openid/userinfo scopes (from the consent
            # screen) on top of what we ask for, so the granted set won't match
            # the requested set. Tell oauthlib to tolerate that instead of
            # raising "Scope has changed".
            os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
            self._flow.fetch_token(authorization_response=full_url)
            creds = self._flow.credentials
            token_file_path().write_text(creds.to_json())
            self._state = "connected"
            return creds
        except Exception as exc:
            self._state = "error"
            self._error = str(exc)
            raise
        finally:
            try:
                self._server.server_close()
            except Exception:
                pass

    def start_background(self) -> str:
        """start() + finish the exchange in a daemon thread; poll status()."""
        if self.has_valid_credentials():
            self._state = "connected"
            return ""
        url = self.start()

        def _run() -> None:
            try:
                self.wait_for_completion()
            except Exception as exc:  # state already set in wait_for_completion
                logger.warning("Calendar authorization failed: %s", exc)

        threading.Thread(target=_run, daemon=True, name="calendar-oauth").start()
        return url
