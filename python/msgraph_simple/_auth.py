"""The redirect listener (ARCHITECTURE.md 11.3).

This is the one place Python does real work, because the core cannot open a browser. It shuttles an
authorization code, which is single-use, short-lived and worthless without the PKCE verifier the
core kept. It never sees a token, and it never validates ``state`` — that happens in the core, so
the CSRF check cannot be skipped by a caller reimplementing this loop.
"""

from __future__ import annotations

import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlsplit

__all__ = ["open_browser", "wait_for_redirect"]

_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Signed in</title></head>
<body style="font-family:system-ui;padding:3rem;text-align:center">
<h1>Signed in</h1><p>You may close this window and return to your terminal.</p>
</body></html>
"""


class _RedirectHandler(BaseHTTPRequestHandler):
    """Accepts exactly one request and records its query string."""

    received: Optional[Dict[str, str]] = None

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        query = parse_qs(urlsplit(self.path).query)
        type(self).received = {key: values[0] for key, values in query.items() if values}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default stderr access log; a library should not narrate."""


def open_browser(authorize_url: str) -> bool:
    """Best effort. A headless box has no browser, and the caller can still open the URL."""
    try:
        return webbrowser.open(authorize_url)
    except Exception:
        return False


def wait_for_redirect(redirect_uri: str, timeout_seconds: float) -> Dict[str, str]:
    """Serve one request on the redirect URI's port and return its query parameters.

    Binds to loopback only, whatever host the redirect URI names.
    """
    port = urlsplit(redirect_uri).port
    if port is None:
        raise ValueError(f"the redirect URI {redirect_uri!r} must name a port")

    _RedirectHandler.received = None

    with HTTPServer(("127.0.0.1", port), _RedirectHandler) as server:
        server.timeout = timeout_seconds
        # Exactly one request: the redirect, and nothing after it.
        server.handle_request()

    received = _RedirectHandler.received
    _RedirectHandler.received = None

    if received is None:
        raise TimeoutError(
            f"no redirect arrived on {redirect_uri} within {timeout_seconds:.0f}s"
        )
    return received
