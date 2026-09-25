"""Plug-and-play Microsoft Graph for Python.

Import one class, hand it credentials, call Graph::

    import asyncio
    from graphplug import GraphClient, Scopes

    async def main():
        async with GraphClient.from_env() as graph:
            await graph.mail.send(to="alice@contoso.com", subject="Hi", body="Hello")

            async for user in graph.paged("/users", select="id,mail"):
                print(user["mail"])

    asyncio.run(main())

Token acquisition and refresh are azure-identity's job; retry, throttling and redirects are
Microsoft's middleware. What this package adds is a surface you can use without reading the Graph
reference first, and a few guarantees the boundary enforces rather than asking you to remember.
"""

from __future__ import annotations

import inspect
import os
from types import TracebackType
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Type

from . import _auth, _operations
from ._errors import GraphError
from ._http import DEFAULT_MAX_CONCURRENCY, Transport
from ._request import build_url, odata, reject_authorization
from ._resources import Calendar, Files, Mail, Teams, Users
from ._scopes import Scopes

__all__ = ["GraphClient", "GraphError", "PendingSignIn", "Scopes"]

__version__ = "0.2.1"


class PendingSignIn:
    """A sign-in waiting on a human.

    ``begin`` has returned what the person must see or do; ``complete`` waits for them.
    """

    def __init__(self, flow: Any, begun: Dict[str, Any], build: Any) -> None:
        self._flow = flow
        self._build = build
        self._settled = False

        #: Device code: the code the person types.
        self.user_code: Optional[str] = begun.get("userCode")
        #: Device code: where they type it.
        self.verification_uri: Optional[str] = begun.get("verificationUri")
        #: Device code: Microsoft's own instruction text, suitable for printing verbatim.
        self.message: Optional[str] = begun.get("message")
        #: Authorization code: the URL to open in a browser.
        self.authorize_url: Optional[str] = begun.get("authorizeUrl")
        #: Authorization code: the anti-forgery value the redirect must echo back.
        self.state: Optional[str] = begun.get("state")
        self.expires_in: int = int(begun.get("expiresInSeconds", 0))

    async def complete(self, **completion: Any) -> "GraphClient":
        """Block until the person finishes, then return a ready client."""
        client = await self._build(self._flow, completion)
        self._settled = True
        return client

    async def cancel(self) -> None:
        """Abandon the sign-in and release whatever it was holding."""
        if not self._settled:
            self._settled = True
            cancel = getattr(self._flow, "cancel", None)
            if cancel is not None:
                await cancel()

    def __repr__(self) -> str:
        what = self.user_code or self.authorize_url or "pending"
        return f"<PendingSignIn {what!r}>"


class GraphClient:
    """One authenticated session.

    Once a client exists, nothing about the request surface depends on how it was authenticated, so
    a script switches between access models by changing one constructor call.

    Not safe to share across event loops. Within one loop it is safe to use concurrently, and
    concurrency is bounded internally so Graph is not overwhelmed.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

        #: Messages in the signed-in user's mailbox.
        self.mail = Mail(self)
        #: Events on the signed-in user's calendar.
        self.calendar = Calendar(self)
        #: Files in the signed-in user's drive.
        self.files = Files(self)
        #: Teams, channels, channel messages and chats.
        self.teams = Teams(self)
        #: People in the directory, and the signed-in person.
        self.users = Users(self)

    # ── application-level access ─────────────────────────────────────────────

    @classmethod
    def app_only(
        cls,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scopes: Optional[Sequence[str]] = None,
        authority_host: Optional[str] = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        _client: Any = None,
    ) -> "GraphClient":
        """Act as the application itself, with admin-consented application permissions.

        These are tenant-wide: ``Mail.Read`` as an application permission reads every mailbox.

        Building a client contacts nothing. The credential is constructed locally and the first
        token is fetched on the first request, so a bad secret surfaces then rather than here.
        """
        credential = _auth.app_only_credential(
            tenant_id, client_id, client_secret, authority_host
        )
        return cls(Transport(
            credential,
            scopes or (_auth.DEFAULT_SCOPE,),
            max_concurrency=max_concurrency,
            client=_client,
        ))

    @classmethod
    def from_env(cls, **overrides: Any) -> "GraphClient":
        """Application-level access from ``AZURE_TENANT_ID`` / ``_CLIENT_ID`` / ``_CLIENT_SECRET``."""
        names = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise GraphError(
                0, "invalidRequest", f"missing environment variables: {', '.join(missing)}"
            )

        tenant, client, secret = (os.environ[name] for name in names)
        return cls.app_only(tenant, client, secret, **overrides)

    @classmethod
    def from_credential(
        cls,
        credential: Any,
        scopes: Optional[Sequence[str]] = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        _client: Any = None,
    ) -> "GraphClient":
        """Use any azure-identity credential, or anything else with a ``get_token``.

        The extension point for the flows this package does not construct itself -- managed
        identity, a certificate, on-behalf-of, a chained credential, or your own. They need no
        support here because nothing above the transport knows how the token was obtained::

            from azure.identity.aio import ManagedIdentityCredential
            graph = GraphClient.from_credential(ManagedIdentityCredential())

        A **synchronous** credential is accepted too and is run on a worker thread. Both spellings
        exist in azure-identity for every flow, the async one is easy to miss, and getting it wrong
        would otherwise fail at the first request with an error about an un-awaited coroutine
        rather than about the credential.

        The credential stays yours: closing the client leaves it open, since it may be shared.
        """
        if credential is None or not callable(getattr(credential, "get_token", None)):
            raise GraphError(
                0, "invalidRequest", "'credential' must have a callable 'get_token'"
            )

        if not inspect.iscoroutinefunction(credential.get_token):
            credential = _auth._SyncCredentialAdapter(credential)

        return cls(Transport(
            credential,
            scopes or (_auth.DEFAULT_SCOPE,),
            max_concurrency=max_concurrency,
            client=_client,
            owns_credential=False,
        ))

    # ── delegated access ─────────────────────────────────────────────────────

    @classmethod
    async def begin_device_code(
        cls,
        tenant_id: str,
        client_id: str,
        scopes: Sequence[str],
        authority_host: Optional[str] = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        _client: Any = None,
    ) -> PendingSignIn:
        """Start a device code sign-in and return at once, so you can render your own prompt."""
        flow, begun = await _auth.device_code_begin(
            tenant_id, client_id, scopes, authority_host
        )

        async def build(pending: Any, _completion: Dict[str, Any]) -> "GraphClient":
            credential = await pending.complete()
            return cls(Transport(
                credential, pending.scopes, max_concurrency=max_concurrency, client=_client
            ))

        return PendingSignIn(flow, begun, build)

    @classmethod
    async def device_code(
        cls,
        tenant_id: str,
        client_id: str,
        scopes: Sequence[str],
        authority_host: Optional[str] = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> "GraphClient":
        """Sign a person in by device code, printing the code and waiting for them."""
        pending = await cls.begin_device_code(
            tenant_id, client_id, scopes, authority_host, max_concurrency
        )
        print(pending.message or f"Visit {pending.verification_uri} and enter {pending.user_code}")

        try:
            return await pending.complete()
        except BaseException:
            await pending.cancel()
            raise

    @classmethod
    async def interactive(
        cls,
        tenant_id: str,
        client_id: str,
        scopes: Sequence[str],
        redirect_uri: str = "http://localhost:8400",
        authority_host: Optional[str] = None,
        timeout_seconds: float = _auth.SIGN_IN_WINDOW_SECONDS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> "GraphClient":
        """Open a browser, catch the redirect on loopback, and return a ready client.

        The redirect URI must match the app registration character for character; a registered
        ``http://localhost:8400`` and a supplied ``http://localhost:8400/`` are different values to
        Entra and produce AADSTS50011.
        """
        scopes = _auth.require_delegated_scopes(scopes)
        verifier, challenge = _auth.pkce_pair()
        state = _auth.new_state()

        url = _auth.authorization_url(
            tenant_id, client_id, scopes, redirect_uri, challenge, state, authority_host
        )

        def open_sign_in() -> None:
            if not _auth.open_browser(url):
                print(f"Open this URL to sign in:\n{url}")

        # The browser opens only once the listener is bound, so a redirect cannot beat it.
        redirect = await _auth.wait_for_redirect(redirect_uri, timeout_seconds, open_sign_in)

        if "error" in redirect:
            raise GraphError(
                0, "signInDeclined", redirect.get("error_description", redirect["error"])
            )
        # Validated here rather than by the caller, so the check cannot be skipped.
        if redirect.get("state") != state:
            raise GraphError(0, "stateMismatch", "the redirect state did not match the one issued")

        credential = await _auth.exchange_code(
            tenant_id, client_id, scopes, redirect_uri, redirect.get("code", ""),
            verifier, authority_host,
        )
        return cls(Transport(credential, scopes, max_concurrency=max_concurrency))

    # ── requests ─────────────────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        version: Optional[str] = None,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        **options: Any,
    ) -> Dict[str, Any]:
        """Issue one request and return the whole envelope, including ``nextLink``.

        ``path`` may be a relative Graph path or a full URL; when it is a URL, ``version`` and the
        OData options are ignored because the URL already carries them.
        """
        url = build_url(path, version, odata(options))
        return await self._transport.json(
            method, url, headers=reject_authorization(headers), body=body
        )

    async def get(self, path: str, **options: Any) -> Any:
        """``GET`` and return the response body."""
        return (await self.request("GET", path, **options)).get("body")

    async def post(self, path: str, body: Any = None, **options: Any) -> Any:
        return (await self.request("POST", path, body=body, **options)).get("body")

    async def patch(self, path: str, body: Any = None, **options: Any) -> Any:
        return (await self.request("PATCH", path, body=body, **options)).get("body")

    async def delete(self, path: str, **options: Any) -> Any:
        return (await self.request("DELETE", path, **options)).get("body")

    def paged(
        self,
        path: str,
        version: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        **options: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Walk every page, yielding items.

        An async generator, so nothing buffers the whole collection and abandoning it half way
        leaks nothing.
        """
        return _operations.paged(
            self._transport,
            build_url(path, version, odata(options)),
            reject_authorization(headers) or None,
        )

    async def batch(
        self, requests: Sequence[_operations.BatchRequest], version: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Send many requests as one call.

        Split at Graph's limit of twenty, dispatched concurrently, and returned in the order you
        sent them. A failing sub-request is reported in place with its own ``status`` rather than
        raised -- one failure must not discard nineteen successes.
        """
        return await _operations.batch(self._transport, requests, version)

    # ── files ────────────────────────────────────────────────────────────────

    async def download(
        self, path: str, dest_path: str, version: Optional[str] = None, **options: Any
    ) -> Dict[str, Any]:
        """Stream a response straight to disk. The directory must already exist."""
        url = build_url(path, version, odata(options))
        return await _operations.download(self._transport, url, dest_path)

    async def upload(
        self, path: str, source_path: str, version: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send a local file, switching to a resumable session above 4 MiB on its own."""
        return await _operations.upload(self._transport, path, source_path, version)

    # ── lifetime ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release the session. Safe to call twice."""
        await self._transport.aclose()

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"<GraphClient {'closed' if self._transport.closed else 'open'}>"
