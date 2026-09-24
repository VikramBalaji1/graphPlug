"""The transport: Microsoft's middleware pipeline, the bearer token, and the concurrency limit.

Everything that touches the wire is here. The rest of the package sees dictionaries.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Mapping, Optional, Sequence

import httpx
from msgraph_core import GraphClientFactory

from . import _log
from ._errors import GraphError, as_graph_error, from_response
from ._request import allowlisted

__all__ = ["Transport", "GRAPH_HOST", "DEFAULT_MAX_CONCURRENCY"]

#: The only host the bearer token may be attached to. Absolute URLs off this host -- a
#: pre-authenticated download URL, say -- still work; they just travel unauthenticated.
GRAPH_HOST = "graph.microsoft.com"

#: Deliberately modest. Graph throttles per application and per tenant, and mailbox operations are
#: limited to a handful of concurrent requests per mailbox, so the ceiling is the service's rather
#: than Python's. Going wider earns 429s, not throughput.
DEFAULT_MAX_CONCURRENCY = 12

_RETRYABLE_HINT = "activityLimitReached"


class Transport:
    """One authenticated HTTP session over Microsoft's middleware pipeline."""

    def __init__(
        self,
        credential: Any,
        scopes: Sequence[str],
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._credential = credential
        self._scopes = tuple(scopes)
        self._gate = asyncio.Semaphore(max_concurrency)
        self.max_concurrency = max_concurrency

        # A caller-supplied client is the test seam: pass one built on httpx.MockTransport and the
        # middleware still wraps it, so tests exercise the real pipeline.
        self._client = GraphClientFactory.create_with_default_middleware(client=client)
        self._closed = False

    # ── the wire ─────────────────────────────────────────────────────────────

    async def send(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
        content: Optional[bytes] = None,
        stream: bool = False,
    ) -> httpx.Response:
        """Issue one request through the pipeline, with the token attached if the host allows it."""
        if self._closed:
            raise GraphError(0, "invalidHandle", "this client has already been closed")

        request = self._client.build_request(
            method, url, headers=dict(headers or {}), json=json_body, content=content
        )
        await self._authorize(request)

        # msgraph-core's transport runs its middleware only when the request carries `options`:
        #
        #     if self.pipeline and hasattr(request, 'options'):
        #
        # That attribute is normally set by Kiota's RequestAdapter, which this package does not
        # use. Without it the whole pipeline -- retry, Retry-After, redirect, telemetry -- is
        # silently skipped and requests go straight to the socket. Setting it here is the one
        # place that undocumented contract is relied upon, and test_transport.py fails loudly if
        # it ever stops working.
        request.options = {}  # type: ignore[attr-defined]

        started = time.monotonic()
        async with self._gate:
            if stream:
                return await self._client.send(request, stream=True)
            response = await self._client.send(request)

        _log.request(
            method,
            url,
            response.status_code,
            int((time.monotonic() - started) * 1000),
            request_id=response.headers.get("request-id"),
            error_code=None if response.is_success else _peek_error_code(response),
        )
        return response

    async def _authorize(self, request: httpx.Request) -> None:
        """Attach the bearer token, but only for Graph itself.

        Kiota's Python middleware has no authorization handler -- unlike .NET, where
        AuthorizationHandler both attached the token and enforced allowed hosts. Both are done
        here, and the host check is what stops a pre-authenticated download URL on some other
        domain being handed a Graph token.
        """
        if request.url.host != GRAPH_HOST:
            return

        token = await self._credential.get_token(*self._scopes)
        request.headers["Authorization"] = f"Bearer {token.token}"

    # ── helpers the rest of the package uses ─────────────────────────────────

    async def json(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        body: Any = None,
        operation: str = "request",
    ) -> Dict[str, Any]:
        """Send, then return the envelope: status, allow-listed headers, body, next link."""
        try:
            response = await self.send(method, url, headers=headers, json_body=body)
        except GraphError:
            raise
        except Exception as exception:
            raise as_graph_error(exception, operation) from exception

        return self.envelope(response)

    @staticmethod
    def envelope(response: httpx.Response) -> Dict[str, Any]:
        """Turn a response into the one shape, raising on failure."""
        headers = allowlisted(response.headers)
        body = _decode(response)

        if not response.is_success:
            raise from_response(response.status_code, headers, body, response.reason_phrase)

        envelope: Dict[str, Any] = {
            "status": response.status_code,
            "headers": headers,
            "body": body,
        }
        if isinstance(body, dict) and body.get("@odata.nextLink"):
            # Promoted so callers never need to know the OData annotation name. Left in the body
            # too, so the Graph response stays verbatim.
            envelope["nextLink"] = body["@odata.nextLink"]
        return envelope

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()
            closer = getattr(self._credential, "close", None)
            if closer is not None:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result

    @property
    def closed(self) -> bool:
        return self._closed


def _decode(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        # Not every Graph failure body is JSON -- a gateway can return HTML.
        return response.text


def _peek_error_code(response: httpx.Response) -> Optional[str]:
    body = _decode(response)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("code")
    return str(response.status_code)
