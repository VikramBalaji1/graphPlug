"""Shared test harness.

The seam is ``httpx.MockTransport``, wrapped by msgraph-core's real middleware pipeline, so tests
exercise the same retry, redirect and telemetry path production does.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.credentials import AccessToken  # noqa: E402

from graphplug import GraphClient  # noqa: E402
from graphplug._http import Transport  # noqa: E402

__all__ = ["Recorder", "FakeCredential", "make_client", "json_response"]

SCOPES = ("https://graph.microsoft.com/.default",)


class FakeCredential:
    """Stands in for azure-identity. Counts acquisitions so re-auth can be asserted."""

    TOKEN = "fake-access-token"

    def __init__(self) -> None:
        self.acquisitions = 0

    async def get_token(self, *scopes: str, **_: Any) -> AccessToken:
        self.acquisitions += 1
        return AccessToken(self.TOKEN, int(time.time()) + 3600)

    async def close(self) -> None:
        return None


class Recorder:
    """Serves scripted responses in order, repeating the last, and records every request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._scripted: List[httpx.Response] = list(responses) or [json_response(200, {})]
        self._index = 0
        self.requests: List[httpx.Request] = []
        #: Set to a callable to answer dynamically instead of from the script.
        self.answer: Optional[Callable[[httpx.Request], httpx.Response]] = None
        self.in_flight = 0
        self.peak_in_flight = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            self.requests.append(request)
            if self.answer is not None:
                return self.answer(request)

            response = self._scripted[min(self._index, len(self._scripted) - 1)]
            self._index += 1
            # A response object cannot be replayed once read, so hand out a fresh one each time.
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
            )
        finally:
            self.in_flight -= 1

    # ── convenience ──────────────────────────────────────────────────────────

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def urls(self) -> List[str]:
        return [str(r.url) for r in self.requests]

    def body(self, index: int = -1) -> Any:
        import json
        return json.loads(self.requests[index].content or b"null")

    def header(self, name: str, index: int = -1) -> Optional[str]:
        return self.requests[index].headers.get(name)


def json_response(status: int, body: Any = None, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def make_client(
    *responses: httpx.Response,
    scopes: Sequence[str] = SCOPES,
    max_concurrency: int = 12,
    credential: Optional[FakeCredential] = None,
) -> "tuple[GraphClient, Recorder, FakeCredential]":
    """A client whose wire is a recorder, with the real middleware in between."""
    recorder = Recorder(*responses)
    inner = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    credential = credential or FakeCredential()
    transport = Transport(credential, scopes, max_concurrency=max_concurrency, client=inner)
    return GraphClient(transport), recorder, credential
