"""Shared behaviour for any Graph collection.

Every Graph collection supports the same operations over a different path, which is a real
variation point rather than a speculative one: a base class plus a thin subclass per resource
means adding a resource later is one class and nothing else moves.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from .. import _operations
from .._request import build_url, odata

__all__ = ["GraphResource"]


class GraphResource:
    """Base for a Graph collection. Subclasses set ``path`` and ``scopes``."""

    #: The collection's path, e.g. ``/me/messages``.
    path: str = ""

    #: The delegated permissions this resource needs, for error messages and scope assembly.
    scopes: Tuple[str, ...] = ()

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── the shared five ──────────────────────────────────────────────────────

    def list(self, **options: Any) -> AsyncIterator[Dict[str, Any]]:
        """Every item, one page at a time. Returns an async generator."""
        return self._client.paged(self.path, **options)

    async def get(self, item_id: str, **options: Any) -> Dict[str, Any]:
        return await self._client.get(f"{self.path}/{item_id}", **options)

    async def create(self, body: Dict[str, Any], **options: Any) -> Dict[str, Any]:
        return await self._client.post(self.path, body=body, **options)

    async def update(self, item_id: str, body: Dict[str, Any], **options: Any) -> Dict[str, Any]:
        return await self._client.patch(f"{self.path}/{item_id}", body=body, **options)

    async def delete(self, item_id: str, **options: Any) -> None:
        await self._client.delete(f"{self.path}/{item_id}", **options)

    # ── the fast path ────────────────────────────────────────────────────────

    async def get_many(
        self, item_ids: Sequence[str], select: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch many items in as few round-trips as Graph allows.

        Chunked into batches of twenty and dispatched concurrently, so two hundred lookups cost ten
        round-trips rather than two hundred. Results come back in the order the ids were given.
        """
        query = f"?{list(odata({'select': select}).items())[0][0]}={select}" if select else ""
        requests = [{"method": "GET", "url": f"{self.path}/{i}{query}"} for i in item_ids]
        return await self._client.batch(requests)

    # ── helpers for subclasses ───────────────────────────────────────────────

    async def _action(self, item_id: str, action: str, body: Any = None) -> Any:
        """POST to an action on one item, e.g. ``/me/messages/{id}/reply``."""
        return await self._client.post(f"{self.path}/{item_id}/{action}", body=body)

    async def _collection_action(self, action: str, body: Any = None) -> Any:
        """POST to an action on the collection's owner, e.g. ``/me/sendMail``."""
        owner = self.path.rsplit("/", 1)[0] or "/me"
        return await self._client.post(f"{owner}/{action}", body=body)

    def _url(self, suffix: str = "", **options: Any) -> str:
        return build_url(f"{self.path}{suffix}", None, odata(options))
