"""Credentials, the sign-in flows, and the loopback redirect listener.

No token handling is written here: acquisition, caching, expiry and refresh are azure-identity's
job. What this module does is choose the right credential and, for the browser flow, keep the PKCE
verifier inside the process.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit

from azure.core.credentials import AccessToken
from azure.identity.aio import ClientSecretCredential

from ._errors import GraphError, as_graph_error

__all__ = [
    "app_only_credential",
    "device_code_begin",
    "authorization_url",
    "exchange_code",
    "open_browser",
    "wait_for_redirect",
    "SIGN_IN_WINDOW_SECONDS",
]

DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com"

#: Matches a device code's own lifetime.
SIGN_IN_WINDOW_SECONDS = 15 * 60


def require_delegated_scopes(scopes: Optional[Sequence[str]]) -> tuple:
    """Delegated flows will not guess.

    There is no safe default: ``.default`` on a delegated flow silently requests every scope ever
    consented for that client, which is the opposite of least privilege.
    """
    if not scopes:
        raise GraphError(0, "invalidRequest", "'scopes' is required for delegated authentication")
    return tuple(scopes)


# ── application-level ────────────────────────────────────────────────────────


def app_only_credential(
    tenant_id: str, client_id: str, client_secret: str, authority: Optional[str] = None
) -> ClientSecretCredential:
    """The app acts as itself, with the application permissions an administrator consented to."""
    for name, value in (("tenant_id", tenant_id), ("client_id", client_id),
                        ("client_secret", client_secret)):
        if not value:
            raise GraphError(0, "invalidRequest", f"'{name}' is required")

    options: Dict[str, Any] = {}
    if authority:
        options["authority"] = authority
    return ClientSecretCredential(tenant_id, client_id, client_secret, **options)


# ── device code ──────────────────────────────────────────────────────────────


class _DeviceCodeSignIn:
    """Device code as two phases: the code is available at once, the token when the user finishes.

    azure.identity.aio has no DeviceCodeCredential -- only the synchronous one exists -- so it runs
    on a worker thread. `authenticate()` blocks until the person completes the sign-in, which is
    exactly what the second phase is waiting for.
    """

    #: The credential class, overridable so the two-phase orchestration can be tested without Entra.
    credential_class: Any = None

    def __init__(self, tenant_id: str, client_id: str, scopes: Sequence[str],
                 authority: Optional[str] = None) -> None:
        self.scopes = require_delegated_scopes(scopes)
        # get_running_loop, not get_event_loop: the latter is deprecated and, outside a running
        # loop, creates one nobody will ever run.
        self._loop = asyncio.get_running_loop()
        self._issued: "asyncio.Future[Dict[str, Any]]" = self._loop.create_future()
        self._signed_in: Optional[asyncio.Task] = None

        options: Dict[str, Any] = {}
        if authority:
            options["authority"] = authority

        if self.credential_class is None:
            from azure.identity import DeviceCodeCredential
            type(self).credential_class = DeviceCodeCredential

        self._credential = self.credential_class(
            client_id=client_id,
            tenant_id=tenant_id,
            prompt_callback=self._on_code,
            **options,
        )

    def _on_code(self, verification_uri: str, user_code: str, expires_on: Any) -> None:
        """Called on the worker thread the moment Microsoft issues the code."""
        payload = {
            "userCode": user_code,
            "verificationUri": verification_uri,
            "message": f"To sign in, open {verification_uri} and enter the code {user_code}",
            "expiresInSeconds": SIGN_IN_WINDOW_SECONDS,
        }
        self._loop.call_soon_threadsafe(
            lambda: None if self._issued.done() else self._issued.set_result(payload)
        )

    async def begin(self) -> Dict[str, Any]:
        self._signed_in = asyncio.create_task(
            asyncio.to_thread(self._credential.authenticate, scopes=list(self.scopes))
        )

        # Wait for the code, but not past a sign-in that fails outright: a bad tenant never
        # reaches the callback, and waiting on it alone would hang until the caller's timeout.
        done, _ = await asyncio.wait(
            {self._issued, self._signed_in}, return_when=asyncio.FIRST_COMPLETED
        )
        if self._issued in done:
            return self._issued.result()

        await self._signed_in  # completed without a code: re-raise the real cause
        raise GraphError(0, "authenticationFailed", "sign-in finished without issuing a device code")

    async def complete(self) -> Any:
        if self._signed_in is None:
            raise GraphError(0, "invalidRequest", "the sign-in was never begun")
        try:
            await asyncio.wait_for(self._signed_in, timeout=SIGN_IN_WINDOW_SECONDS)
        except asyncio.TimeoutError:
            raise GraphError(
                0, "signInTimeout", "the user did not complete sign-in within the window"
            ) from None
        except Exception as exception:
            raise as_graph_error(exception, "device_code") from exception

        return _SyncCredentialAdapter(self._credential)

    async def cancel(self) -> None:
        if self._signed_in is not None and not self._signed_in.done():
            self._signed_in.cancel()
            try:
                await self._signed_in
            except (asyncio.CancelledError, Exception):
                pass


async def device_code_begin(
    tenant_id: str, client_id: str, scopes: Sequence[str], authority: Optional[str] = None
) -> "tuple[_DeviceCodeSignIn, Dict[str, Any]]":
    flow = _DeviceCodeSignIn(tenant_id, client_id, scopes, authority)
    return flow, await flow.begin()


class _SyncCredentialAdapter:
    """Presents a synchronous azure-identity credential as an async one."""

    def __init__(self, credential: Any) -> None:
        self._credential = credential

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return await asyncio.to_thread(self._credential.get_token, *scopes, **kwargs)

    async def close(self) -> None:
        closer = getattr(self._credential, "close", None)
        if closer is not None:
            await asyncio.to_thread(closer)


# ── authorization code with PKCE ─────────────────────────────────────────────


def pkce_pair() -> "tuple[str, str]":
    """An RFC 7636 verifier and its S256 challenge.

    The verifier never leaves this process. It is credential material for the duration of the
    exchange, and keeping it here means nothing can leak it by logging a return value.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def new_state() -> str:
    """The anti-forgery value. This one does travel -- that is what it is for."""
    return _b64url(secrets.token_bytes(32))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def authorization_url(
    tenant_id: str,
    client_id: str,
    scopes: Sequence[str],
    redirect_uri: str,
    challenge: str,
    state: str,
    authority: Optional[str] = None,
) -> str:
    # offline_access is requested here so the session can refresh silently for the life of the
    # client. It is deliberately NOT passed to MSAL's token call, which rejects reserved scopes.
    requested = " ".join([*scopes, "offline_access"])
    query = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": requested,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{(authority or DEFAULT_AUTHORITY).rstrip('/')}/{tenant_id}/oauth2/v2.0/authorize?{query}"


async def exchange_code(
    tenant_id: str,
    client_id: str,
    scopes: Sequence[str],
    redirect_uri: str,
    code: str,
    verifier: str,
    authority: Optional[str] = None,
) -> Any:
    """Redeem the authorization code for a token, proving possession with the PKCE verifier.

    Uses MSAL rather than azure-identity's AuthorizationCodeCredential, which has no
    ``code_verifier`` parameter. MSAL is synchronous, so the exchange runs on a worker thread.
    """
    import msal

    application = msal.PublicClientApplication(
        client_id, authority=f"{(authority or DEFAULT_AUTHORITY).rstrip('/')}/{tenant_id}"
    )

    result = await asyncio.to_thread(
        application.acquire_token_by_authorization_code,
        code,
        scopes=list(scopes),
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )

    if "access_token" not in result:
        detail = result.get("error_description") or result.get("error") or "sign-in failed"
        raise GraphError(0, _msal_code(detail), detail)

    return _MsalCredential(application, result, tuple(scopes))


def _msal_code(detail: str) -> str:
    from ._errors import code_for_exception
    return code_for_exception(GraphError(0, "authenticationFailed", detail))


class _MsalCredential:
    """A credential over MSAL's token cache.

    No token handling is written here either: the cache was seeded by the code exchange, and
    ``acquire_token_silent`` refreshes from it.
    """

    def __init__(self, application: Any, result: Dict[str, Any], scopes: Sequence[str]) -> None:
        self._application = application
        self._scopes = list(scopes)
        self._account = result.get("id_token_claims") and application.get_accounts()
        self._last = result

    async def get_token(self, *scopes: str, **_: Any) -> AccessToken:
        wanted = list(scopes) or self._scopes
        accounts = self._application.get_accounts()
        result = await asyncio.to_thread(
            self._application.acquire_token_silent, wanted, accounts[0] if accounts else None
        )
        if not result or "access_token" not in result:
            raise GraphError(
                0,
                "interactionRequired",
                "the signed-in session can no longer be refreshed; sign in again",
            )
        import time as _time
        return AccessToken(result["access_token"], int(_time.time()) + int(result.get("expires_in", 3600)))

    async def close(self) -> None:
        return None


# ── the loopback redirect listener ───────────────────────────────────────────

_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Signed in</title></head>
<body style="font-family:system-ui;padding:3rem;text-align:center">
<h1>Signed in</h1><p>You may close this window and return to your terminal.</p>
</body></html>
"""


class _RedirectHandler(BaseHTTPRequestHandler):
    received: Optional[Dict[str, str]] = None

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        query = parse_qs(urlsplit(self.path).query)
        type(self).received = {k: v[0] for k, v in query.items() if v}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default access log; a library should not narrate."""


def open_browser(url: str) -> bool:
    """Best effort. A headless box has no browser, and the caller can still open the URL."""
    try:
        return webbrowser.open(url)
    except Exception:
        return False


async def wait_for_redirect(redirect_uri: str, timeout_seconds: float) -> Dict[str, str]:
    """Serve exactly one request on the redirect URI's port and return its query parameters.

    Binds to loopback only, whatever host the redirect URI names.
    """
    port = urlsplit(redirect_uri).port
    if port is None:
        raise GraphError(0, "invalidRequest", f"the redirect URI {redirect_uri!r} must name a port")

    def serve() -> Optional[Dict[str, str]]:
        _RedirectHandler.received = None
        with HTTPServer(("127.0.0.1", port), _RedirectHandler) as server:
            server.timeout = timeout_seconds
            server.handle_request()
        received, _RedirectHandler.received = _RedirectHandler.received, None
        return received

    received = await asyncio.to_thread(serve)
    if received is None:
        raise GraphError(
            0, "signInTimeout", f"no redirect arrived on {redirect_uri} within {timeout_seconds:.0f}s"
        )
    return received
