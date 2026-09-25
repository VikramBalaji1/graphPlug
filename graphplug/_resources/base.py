"""Shared behaviour for any Graph collection.

Every Graph collection supports the same operations over a different path, which is a real
variation point rather than a speculative one: a base class plus a thin subclass per resource
means adding a resource later is one class and nothing else moves.

Collections that belong to a person are written as ``/me/...``. Every method takes ``user``, and
``_for`` re-points the path at ``/users/{user}/...`` when one is given. That is what lets an
app-only client, which has no ``/me``, send mail or book a calendar on behalf of a named mailbox.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from .._request import segment

__all__ = ["GraphResource"]

_ME = "/me"


class GraphResource:
    """Base for a Graph collection. Subclasses set ``path`` and ``scopes``."""

    #: The collection's path, e.g. ``/me/messages``.
    path: str = ""

    #: The delegated permissions this resource needs, for error messages and scope assembly.
    scopes: Tuple[str, ...] = ()

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _for(path: str, user: Optional[str]) -> str:
        """Point a ``/me`` path at another person: ``/me/events`` becomes ``/users/{user}/events``.

        ``user`` is an id, a user principal name or a mail address. ``None`` or ``"me"`` keeps
        ``/me``, the signed-in person. Paths not under ``/me`` (``/teams``, ``/users``) are
        returned unchanged.
        """
        if user is None or user == "me":
            return path
        if path != _ME and not path.startswith(_ME + "/"):
            return path
        return f"/users/{segment(user)}{path[len(_ME):]}"

    # ── the shared five ──────────────────────────────────────────────────────

    def list(self, user: Optional[str] = None, **options: Any) -> AsyncIterator[Dict[str, Any]]:
        """Every item, one page at a time. Returns an async generator."""
        return self._client.paged(self._for(self.path, user), **options)

    async def get(self, item_id: str, user: Optional[str] = None, **options: Any) -> Dict[str, Any]:
        return await self._client.get(self._item(item_id, user), **options)

    async def create(
        self, body: Dict[str, Any], user: Optional[str] = None, **options: Any
    ) -> Dict[str, Any]:
        return await self._client.post(self._for(self.path, user), body=body, **options)

    async def update(
        self, item_id: str, body: Dict[str, Any], user: Optional[str] = None, **options: Any
    ) -> Dict[str, Any]:
        return await self._client.patch(self._item(item_id, user), body=body, **options)

    async def delete(self, item_id: str, user: Optional[str] = None, **options: Any) -> None:
        await self._client.delete(self._item(item_id, user), **options)

    # ── the fast path ────────────────────────────────────────────────────────

    async def get_many(
        self, item_ids: Sequence[str], select: Optional[str] = None, user: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch many items in as few round-trips as Graph allows.

        Chunked into batches of twenty and dispatched concurrently, so two hundred lookups cost ten
        round-trips rather than two hundred. Results come back in the order the ids were given.
        """
        query = f"?$select={select}" if select else ""
        requests = [{"method": "GET", "url": f"{self._item(i, user)}{query}"} for i in item_ids]
        return await self._client.batch(requests)

    # ── helpers for subclasses ───────────────────────────────────────────────

    def _item(self, item_id: str, user: Optional[str] = None) -> str:
        """One item in the collection, e.g. ``/me/messages/{id}``."""
        return f"{self._for(self.path, user)}/{segment(item_id)}"

    async def _action(
        self, item_id: str, action: str, body: Any = None, user: Optional[str] = None
    ) -> Any:
        """POST to an action on one item, e.g. ``/me/messages/{id}/reply``."""
        return await self._client.post(f"{self._item(item_id, user)}/{action}", body=body)

    async def _collection_action(
        self, action: str, body: Any = None, user: Optional[str] = None
    ) -> Any:
        """POST to an action on the collection's owner, e.g. ``/me/sendMail``."""
        owner = self.path.rsplit("/", 1)[0] or _ME
        return await self._client.post(f"{self._for(owner, user)}/{action}", body=body)
