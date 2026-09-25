"""OneDrive and SharePoint files.

The thing that makes Graph's file API hard to write against is addressing. The same item is
``/me/drive/items/01ABC...`` by id and ``/me/drive/root:/reports/q3.xlsx:`` by path -- with a colon
opening the path segment and *another* colon closing it before whatever comes next. Forgetting the
trailing colon gives a 400 that says nothing about colons.

``_address`` decides between the two on one rule: a leading slash means a path, anything else is an
id. Every method here takes either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Union

from .._errors import GraphError
from .._scopes import Scopes
from .base import GraphResource

__all__ = ["Files"]

DRIVE = "/me/drive"

DEFAULT_FIELDS = "id,name,size,webUrl,lastModifiedDateTime,file,folder"

#: Graph's own names for what happens when a new folder's name is taken.
_CONFLICT = ("rename", "replace", "fail")

#: Who a sharing link works for. "anonymous" is frequently disabled by tenant policy.
_SCOPES = ("anonymous", "organization", "users")

_LINK_KINDS = ("view", "edit", "embed")


class Files(GraphResource):
    """Items in the signed-in user's drive.

    Every method takes either a drive path (``"/reports/q3.xlsx"``, leading slash) or an item id.
    """

    path = f"{DRIVE}/items"
    scopes = Scopes.FILES_READ_WRITE

    @staticmethod
    def _address(item: str) -> str:
        """The endpoint for one item, by path or by id."""
        if not item or not item.strip():
            raise GraphError(0, "invalidRequest", "'item' is required")
        if not item.startswith("/"):
            return f"{DRIVE}/items/{item}"
        if item == "/":
            return f"{DRIVE}/root"
        # The closing colon is what separates the path from whatever follows it.
        return f"{DRIVE}/root:{item.rstrip('/')}:"

    # ── moving bytes ─────────────────────────────────────────────────────────

    async def upload(self, source: Union[str, Path], to: Optional[str] = None) -> Dict[str, Any]:
        """Send a local file to the drive, replacing whatever is at the destination.

        ``to`` is the destination drive path; omit it and the file keeps its own name at the root.
        Files above 4 MiB switch to a resumable upload session on their own -- the caller never
        picks a strategy, and the path is the same either way.
        """
        local = Path(source)
        if not local.is_file():
            raise GraphError(0, "invalidRequest", f"'{source}' does not exist")

        destination = to or f"/{local.name}"
        if not destination.startswith("/"):
            destination = "/" + destination

        return await self._client.upload(f"{self._address(destination)}/content", str(local))

    async def download(self, item: str, dest_path: Union[str, Path]) -> Dict[str, Any]:
        """Stream a file to disk. The destination directory must already exist.

        Graph answers the content endpoint with a redirect to a short-lived, pre-authenticated URL
        on another host. The transport follows it and deliberately does not attach the bearer token
        to that second hop.
        """
        return await self._client.download(f"{self._address(item)}/content", str(dest_path))

    # ── looking around ───────────────────────────────────────────────────────

    def folder(
        self, path: str = "/", select: str = DEFAULT_FIELDS, top: int = 200
    ) -> AsyncIterator[Dict[str, Any]]:
        """Everything directly inside a folder. Defaults to the drive root."""
        return self._client.paged(f"{self._address(path)}/children", select=select, top=top)

    def search(self, query: str, select: str = DEFAULT_FIELDS) -> AsyncIterator[Dict[str, Any]]:
        """Search the whole drive by name and content."""
        if not query:
            raise GraphError(0, "invalidRequest", "'query' is required")
        # Graph spells this one as a function on the path, not as $search.
        escaped = query.replace("'", "''")
        return self._client.paged(f"{DRIVE}/root/search(q='{escaped}')", select=select)

    async def metadata(self, item: str, select: str = DEFAULT_FIELDS) -> Dict[str, Any]:
        """One item's properties, without its bytes."""
        return await self._client.get(self._address(item), select=select)

    # ── changing things ──────────────────────────────────────────────────────

    async def make_folder(self, path: str, conflict: str = "fail") -> Dict[str, Any]:
        """Create a folder. ``path`` is the full drive path of the folder to create."""
        if not path.startswith("/"):
            path = "/" + path
        parent, _, name = path.rstrip("/").rpartition("/")
        if not name:
            raise GraphError(0, "invalidRequest", "'path' must name the folder to create")

        if conflict not in _CONFLICT:
            raise GraphError(
                0, "invalidRequest", f"'conflict' must be one of {', '.join(_CONFLICT)}"
            )

        return await self._client.post(f"{self._address(parent or '/')}/children", body={
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": conflict,
        })

    async def remove(self, item: str) -> None:
        """Move an item to the recycle bin."""
        await self._client.delete(self._address(item))

    async def share_link(
        self, item: str, kind: str = "view", scope: str = "organization"
    ) -> str:
        """Create a sharing link and return just the URL.

        ``scope="anonymous"`` produces a link anyone can open, and is frequently disabled by tenant
        policy -- that arrives as an ``accessDenied`` error rather than a working link.
        """
        if kind not in _LINK_KINDS:
            raise GraphError(
                0, "invalidRequest", f"'kind' must be one of {', '.join(_LINK_KINDS)}"
            )
        if scope not in _SCOPES:
            raise GraphError(0, "invalidRequest", f"'scope' must be one of {', '.join(_SCOPES)}")

        created = await self._client.post(
            f"{self._address(item)}/createLink", body={"type": kind, "scope": scope}
        )
        return ((created or {}).get("link") or {}).get("webUrl", "")
