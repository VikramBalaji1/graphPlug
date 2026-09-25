"""People and the directory.

Two things make this endpoint awkward, and both live here rather than at the call site.

The first is that the signed-in person is ``/me`` while everyone else is ``/users/{id}``, so every
directory call has a fork in it. ``_who`` closes it: pass nobody and you get yourself.

The second is that Graph refuses its own most useful queries -- ``$search``, ``$count``,
``endswith``, ``$filter`` combined with ``$orderby`` -- unless the request carries
``ConsistencyLevel: eventual``. Without it the failure is a bare 400 that names none of this.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

from .._errors import GraphError
from .._request import segment
from .._scopes import Scopes
from .base import GraphResource

__all__ = ["Users"]

#: What Graph calls an advanced query. Cheap to send, and the queries below do not work without it.
ADVANCED_QUERY = {"ConsistencyLevel": "eventual"}

DEFAULT_FIELDS = "id,displayName,mail,userPrincipalName,jobTitle,department"


class Users(GraphResource):
    """People in the directory, and the signed-in person.

    Works under both access models. Under application-level access ``/me`` does not exist, so the
    methods that default to it need a user id.
    """

    path = "/users"
    scopes = Scopes.USER_READ_ALL

    @staticmethod
    def _who(user: Optional[str]) -> str:
        """``/me`` for the signed-in person, ``/users/{id}`` for anyone else."""
        if user is None or user == "me":
            return "/me"
        return f"/users/{segment(user)}"

    # ── the signed-in person ─────────────────────────────────────────────────

    async def me(self, select: str = DEFAULT_FIELDS) -> Dict[str, Any]:
        """Who am I. Delegated access only -- there is no user under app permissions."""
        return await self._client.get("/me", select=select)

    # ── finding someone ──────────────────────────────────────────────────────

    def find(
        self, query: str, top: int = 25, select: str = DEFAULT_FIELDS
    ) -> AsyncIterator[Dict[str, Any]]:
        """Search names and addresses for a fragment.

        Graph's ``$search`` on users matches per field, so the term is applied to both the display
        name and the mail address -- searching "smith" should find someone whether it is their
        surname or their address.
        """
        if not query:
            raise GraphError(0, "invalidRequest", "'query' is required")

        term = query.replace('"', "")
        return self._client.paged(
            self.path,
            headers=dict(ADVANCED_QUERY),
            search=f'"displayName:{term}" OR "mail:{term}"',
            select=select,
            top=top,
        )

    async def by_email(self, address: str, select: str = DEFAULT_FIELDS) -> Dict[str, Any]:
        """Look someone up by address.

        Not the same as ``get(address)``: that resolves the *user principal name*, which is often
        but not always the mail address. This filters on ``mail`` itself and raises if nobody
        matches, so a wrong answer is not silently returned.
        """
        quoted = address.replace("'", "''")  # OData's escape; o'brien@ is a valid address
        page = await self._client.get(
            self.path, filter=f"mail eq '{quoted}'", select=select, top=2
        )
        found = (page or {}).get("value", [])
        if not found:
            raise GraphError(0, "itemNotFound", f"no user has the mail address '{address}'")
        return found[0]

    # ── the org chart ────────────────────────────────────────────────────────

    async def manager(self, user: Optional[str] = None) -> Dict[str, Any]:
        """Who this person reports to. Raises ``itemNotFound`` if nobody is set."""
        return await self._client.get(f"{self._who(user)}/manager")

    def reports(
        self, user: Optional[str] = None, select: str = DEFAULT_FIELDS
    ) -> AsyncIterator[Dict[str, Any]]:
        """Who reports to this person, directly."""
        return self._client.paged(f"{self._who(user)}/directReports", select=select)

    def groups(
        self, user: Optional[str] = None, select: str = "id,displayName,mail,groupTypes"
    ) -> AsyncIterator[Dict[str, Any]]:
        """Every group this person belongs to, directly."""
        return self._client.paged(f"{self._who(user)}/memberOf", select=select)

    # ── photo ────────────────────────────────────────────────────────────────

    async def photo(
        self, dest_path: str, user: Optional[str] = None, size: Optional[str] = None
    ) -> Dict[str, Any]:
        """Save a profile photo to disk.

        ``size`` is one of Graph's fixed sizes such as ``"96x96"``; omit it for the original. A
        person with no photo is a 404, which arrives as ``GraphError`` with ``itemNotFound``.
        """
        endpoint = f"{self._who(user)}/photo" if not size else f"{self._who(user)}/photos/{size}"
        return await self._client.download(f"{endpoint}/$value", dest_path)
