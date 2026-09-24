"""Plug-and-play Microsoft Graph from Python.

Import one class, hand it your credentials, and call Graph::

    from msgraph_simple import GraphClient

    with GraphClient.app_only(tenant_id=..., client_id=..., client_secret=...) as g:
        for user in g.paged("/users", select="id,displayName,mail"):
            print(user["mail"])

Everything behind that — credential construction, token acquisition and refresh, the retry and
throttling pipeline, pagination, batching, chunked uploads, error normalisation — lives in the C#
core. This layer contains no Graph knowledge: no endpoint lists, no retry logic, no error
interpretation beyond raising what the core reports.
"""

from __future__ import annotations

import os
from types import TracebackType
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Type, Union

from . import _auth
from ._native import GraphError, call, dumps

__all__ = ["GraphClient", "GraphError", "PendingSignIn"]

__version__ = "0.1.0"

#: OData parameters, spelled as Python keywords. The core URL-encodes them.
_ODATA_KEYWORDS = ("select", "filter", "top", "skip", "expand", "orderby", "search", "count")

#: How long ``interactive()`` waits for the browser round-trip, matching the core's sign-in window.
_SIGN_IN_TIMEOUT_SECONDS = 15 * 60

BatchRequest = Union[Tuple[str, str], Dict[str, Any]]


def _odata(options: Dict[str, Any]) -> Dict[str, Any]:
    """Turn ``select=...`` into ``$select``, leaving anything else as a literal parameter."""
    query: Dict[str, Any] = {}
    for name, value in options.items():
        if value is None:
            continue
        query[f"${name}" if name in _ODATA_KEYWORDS else name] = value
    return query


class PendingSignIn:
    """A sign-in waiting on a human (7.5).

    ``begin`` has returned what the user must see or do; ``complete`` waits for them.
    """

    def __init__(self, envelope: Dict[str, Any]) -> None:
        self._envelope = envelope
        self._settled = False

        self.flow_id: int = int(envelope["flowId"])
        #: Device code: the code the user types.
        self.user_code: Optional[str] = envelope.get("userCode")
        #: Device code: where they type it.
        self.verification_uri: Optional[str] = envelope.get("verificationUri")
        #: Device code: Microsoft's own instruction text, suitable for printing verbatim.
        self.message: Optional[str] = envelope.get("message")
        #: Authorization code: the URL to open in a browser.
        self.authorize_url: Optional[str] = envelope.get("authorizeUrl")
        #: Authorization code: the anti-forgery value the redirect must echo back.
        self.state: Optional[str] = envelope.get("state")
        self.expires_in: int = int(envelope.get("expiresInSeconds", 0))

    def complete(self, **completion: Any) -> "GraphClient":
        """Block until the user finishes, then return a ready client."""
        payload = dumps(completion) if completion else None
        envelope = call("graph_auth_complete", self.flow_id, payload)
        self._settled = True
        return GraphClient(int(envelope["handle"]))

    def cancel(self) -> None:
        """Abandon the sign-in and release whatever it was holding."""
        if self._settled:
            return
        self._settled = True
        call("graph_auth_cancel", self.flow_id)

    def __repr__(self) -> str:
        what = self.user_code or self.authorize_url or "pending"
        return f"<PendingSignIn flow_id={self.flow_id} {what!r}>"


class GraphClient:
    """One authenticated session.

    Once a client exists, nothing about the request surface depends on how it was authenticated;
    scripts switch between access models by changing one constructor call.

    Safe to share across threads. Creating one per thread is supported but wasteful, and for
    delegated access it is worse than wasteful, because each would prompt its own sign-in.
    """

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._closed = False

    # ── application-level access ─────────────────────────────────────────────

    @classmethod
    def app_only(
        cls,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scopes: Optional[Sequence[str]] = None,
        authority_host: Optional[str] = None,
        max_retries: Optional[int] = None,
        max_delay_seconds: Optional[int] = None,
    ) -> "GraphClient":
        """Act as the application itself, with admin-consented application permissions.

        These are tenant-wide: ``Mail.Read`` as an application permission reads every mailbox.
        """
        credentials: Dict[str, Any] = {
            "type": "clientSecret",
            "tenantId": tenant_id,
            "clientId": client_id,
            "clientSecret": client_secret,
        }
        _add_optional(credentials, scopes, authority_host, max_retries, max_delay_seconds)

        return cls(int(call("graph_client_create", dumps(credentials))["handle"]))

    @classmethod
    def from_env(cls, **overrides: Any) -> "GraphClient":
        """Application-level access from ``AZURE_TENANT_ID`` / ``_CLIENT_ID`` / ``_CLIENT_SECRET``.

        The secret is read here, passed once across the boundary, and lives thereafter only inside
        the credential the core holds.
        """
        missing = [
            name
            for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            raise GraphError(0, "invalidRequest", f"missing environment variables: {', '.join(missing)}")

        return cls.app_only(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
            **overrides,
        )

    # ── delegated access ─────────────────────────────────────────────────────

    @classmethod
    def begin_device_code(
        cls,
        tenant_id: str,
        client_id: str,
        scopes: Sequence[str],
        authority_host: Optional[str] = None,
    ) -> PendingSignIn:
        """Start a device code sign-in and return at once, for custom prompting."""
        credentials: Dict[str, Any] = {
            "type": "deviceCode",
            "tenantId": tenant_id,
            "clientId": client_id,
            "scopes": list(scopes),
        }
        _add_optional(credentials, None, authority_host, None, None)

        return PendingSignIn(call("graph_auth_begin", dumps(credentials)))

    @classmethod
    def device_code(
        cls,
        tenant_id: str,
        client_id: str,
        scopes: Sequence[str],
        authority_host: Optional[str] = None,
    ) -> "GraphClient":
        """Sign a user in by device code, printing the code and blocking until they finish."""
        flow = cls.begin_device_code(tenant_id, client_id, scopes, authority_host)
        print(flow.message or f"Visit {flow.verification_uri} and enter {flow.user_code}")

        try:
            return flow.complete()
        except BaseException:
            flow.cancel()
            raise

    @classmethod
    def _begin_interactive(
        cls,
        tenant_id: str,
        client_id: str,
        scopes: Sequence[str],
        redirect_uri: str = "http://localhost:8400",
        authority_host: Optional[str] = None,
    ) -> PendingSignIn:
        """Start an authorization-code sign-in and return the URL to open."""
        credentials: Dict[str, Any] = {
            "type": "authorizationCode",
            "tenantId": tenant_id,
            "clientId": client_id,
            "scopes": list(scopes),
            "redirectUri": redirect_uri,
        }
        _add_optional(credentials, None, authority_host, None, None)

        return PendingSignIn(call("graph_auth_begin", dumps(credentials)))

    @classmethod
    def interactive(
        cls,
        tenant_id: str,
        client_id: str,
        scopes: Sequence[str],
        redirect_uri: str = "http://localhost:8400",
        authority_host: Optional[str] = None,
        timeout_seconds: float = _SIGN_IN_TIMEOUT_SECONDS,
    ) -> "GraphClient":
        """Open a browser, catch the redirect, and return a ready client.

        The listener binds to loopback and accepts exactly one request. ``state`` travels back to
        the core for validation rather than being checked here.
        """
        flow = cls._begin_interactive(tenant_id, client_id, scopes, redirect_uri, authority_host)

        try:
            if not _auth.open_browser(flow.authorize_url or ""):
                print(f"Open this URL to sign in:\n{flow.authorize_url}")

            redirect = _auth.wait_for_redirect(redirect_uri, timeout_seconds)

            if "error" in redirect:
                raise GraphError(
                    0,
                    "signInDeclined",
                    redirect.get("error_description", redirect["error"]),
                )

            return flow.complete(code=redirect.get("code"), state=redirect.get("state"))
        except BaseException:
            flow.cancel()
            raise

    # ── requests ─────────────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        version: Optional[str] = None,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: Optional[int] = None,
        **options: Any,
    ) -> Dict[str, Any]:
        """Issue one request and return the whole envelope, including ``nextLink``.

        ``path`` may be a relative Graph path or an absolute URL; when absolute, ``version`` and
        the OData options are ignored because the URL already carries them.
        """
        return call("graph_request", self._require_open(), dumps(self._envelope(
            method, path, version, body, headers, timeout_ms, options)))

    def get(self, path: str, **options: Any) -> Any:
        """``GET`` and return the response body."""
        return self.request("GET", path, **options).get("body")

    def post(self, path: str, body: Any = None, **options: Any) -> Any:
        return self.request("POST", path, body=body, **options).get("body")

    def patch(self, path: str, body: Any = None, **options: Any) -> Any:
        return self.request("PATCH", path, body=body, **options).get("body")

    def delete(self, path: str, **options: Any) -> Any:
        return self.request("DELETE", path, **options).get("body")

    def paged(self, path: str, **options: Any) -> Iterator[Dict[str, Any]]:
        """Walk every page, yielding items.

        A generator is the language-native equivalent of the ``IAsyncEnumerable`` the core uses
        internally. There is no cursor state in the native library, so abandoning this half way
        leaks nothing.
        """
        response = self.request("GET", path, **options)
        while True:
            yield from (response.get("body") or {}).get("value", [])

            next_link = response.get("nextLink")
            if not next_link:
                return
            # The next link is a complete, self-describing cursor; echo it straight back.
            response = self.request("GET", next_link)

    def batch(self, requests: Iterable[BatchRequest], **options: Any) -> List[Dict[str, Any]]:
        """Send many requests as one.

        The core chunks at Graph's limit of 20 and restores submission order, so results align
        positionally with what was sent. A failing sub-request is reported in place with its own
        ``status``, never raised — one failure must not discard its siblings.
        """
        prepared: List[Dict[str, Any]] = []
        for index, request in enumerate(requests):
            if isinstance(request, tuple):
                method, url = request
                request = {"method": method, "url": url}
            prepared.append({"id": str(index), **request})

        response = self.request("POST", "/$batch", body={"requests": prepared}, **options)
        return (response.get("body") or {}).get("responses", [])

    # ── files ────────────────────────────────────────────────────────────────

    def download(self, path: str, dest_path: str, **options: Any) -> Dict[str, Any]:
        """Stream a Graph response straight to ``dest_path``.

        The directory must already exist. The bytes never enter a JSON envelope and never fully
        enter memory, so file size is bounded by disk rather than RAM.
        """
        envelope = self._envelope("GET", path, options.pop("version", None), None,
                                  options.pop("headers", None), options.pop("timeout_ms", None),
                                  options)
        envelope["destPath"] = dest_path

        return call("graph_download", self._require_open(), dumps(envelope))

    def upload(self, path: str, source_path: str, **options: Any) -> Dict[str, Any]:
        """Send a local file, switching to a chunked upload session above 4 MiB."""
        envelope = self._envelope("PUT", path, options.pop("version", None), None,
                                  options.pop("headers", None), options.pop("timeout_ms", None),
                                  options)
        envelope["sourcePath"] = source_path

        return call("graph_upload", self._require_open(), dumps(envelope))

    # ── lifetime ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release the session. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        call("graph_client_close", self._handle)

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<GraphClient handle={self._handle} {state}>"

    # ── internals ────────────────────────────────────────────────────────────

    def _require_open(self) -> int:
        if self._closed:
            raise GraphError(0, "invalidHandle", "this client has already been closed")
        return self._handle

    @staticmethod
    def _envelope(
        method: str,
        path: str,
        version: Optional[str],
        body: Any,
        headers: Optional[Dict[str, str]],
        timeout_ms: Optional[int],
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {"method": method, "path": path}

        query = _odata(options)
        if query:
            envelope["query"] = query
        if version is not None:
            envelope["version"] = version
        if body is not None:
            envelope["body"] = body
        if headers:
            envelope["headers"] = headers
        if timeout_ms is not None:
            envelope["timeoutMs"] = timeout_ms

        return envelope


def _add_optional(
    credentials: Dict[str, Any],
    scopes: Optional[Sequence[str]],
    authority_host: Optional[str],
    max_retries: Optional[int],
    max_delay_seconds: Optional[int],
) -> None:
    if scopes:
        credentials["scopes"] = list(scopes)
    if authority_host:
        credentials["authorityHost"] = authority_host

    retry = {
        key: value
        for key, value in (("maxRetries", max_retries), ("maxDelaySeconds", max_delay_seconds))
        if value is not None
    }
    if retry:
        credentials["retry"] = retry
