"""Building requests and shaping responses.

Ported from the C# core's `GraphUrlBuilder`, `ResponseHeaderFilter` and `GraphOperation`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote, urlencode

from ._errors import GraphError

__all__ = ["build_url", "odata", "segment", "allowlisted", "reject_authorization", "DEFAULT_VERSION"]

BASE_URL = "https://graph.microsoft.com"
DEFAULT_VERSION = "v1.0"
SUPPORTED_VERSIONS = (DEFAULT_VERSION, "beta")

#: What tells a whole URL apart from a Graph path: a scheme and ``://`` at the very start.
#:
#: Deliberately not ``urlsplit(path).scheme``, which reads "C:\\secrets" as scheme "c". Anchored at
#: the start because a Graph path may carry ``://`` further in -- a drive search for a URL, say.
#: The equivalent check in the C# core was the fix for a bug that broke every request on Linux
#: while passing on Windows.
_ABSOLUTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

#: Left alone when an id or a drive path goes into a URL path. Everything else -- ``#``, ``?``,
#: ``%``, spaces -- is percent-encoded, or it would end the path early.
_PATH_SAFE = "/:@=+$,!"

#: OData parameters, spelled as Python keywords.
ODATA_KEYWORDS = ("select", "filter", "top", "skip", "expand", "orderby", "search", "count")

#: Response headers reach the caller through an allowlist, never a denylist: a denylist fails open
#: on whatever header Microsoft adds tomorrow. Authorization and WWW-Authenticate therefore cannot
#: escape even by accident.
_HEADER_ALLOWLIST = (
    "request-id",
    "client-request-id",
    "Date",
    "Retry-After",
    "Content-Type",
    "Location",
    "ETag",
)


def odata(options: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn ``select=...`` into ``$select``, leaving anything else a literal parameter."""
    query: Dict[str, Any] = {}
    for name, value in options.items():
        if value is None:
            continue
        query[f"${name}" if name in ODATA_KEYWORDS else name] = value
    return query


def build_url(
    path: Optional[str],
    version: Optional[str] = None,
    query: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build the request URL.

    An absolute ``path`` is used verbatim and ``version`` and ``query`` are ignored, because the
    URL already carries them. That is what makes ``@odata.nextLink`` echo-back and pre-authenticated
    download URLs work with no special case.
    """
    if not path or not path.strip():
        raise GraphError(0, "invalidRequest", "'path' is required")

    if _ABSOLUTE.match(path):
        if not path.lower().startswith("https://"):
            raise GraphError(0, "invalidRequest", "an absolute 'path' must use https")
        return path

    resolved = _resolve_version(version)
    suffix = path if path.startswith("/") else "/" + path
    url = f"{BASE_URL}/{resolved}{suffix}"

    if query:
        # safe="$" keeps $select readable on the wire; Graph accepts either form.
        encoded = urlencode({k: _literal(v) for k, v in query.items()}, safe="$")
        url = f"{url}{'&' if '?' in url else '?'}{encoded}"
    return url


def _literal(value: Any) -> str:
    """OData is case-sensitive about booleans: ``$count=true``, never ``True``."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def segment(value: str) -> str:
    """Percent-encode an id or drive path for use inside a URL path.

    A guest's user principal name carries ``#EXT#`` and a file may be called ``Q3 #1.xlsx``.
    Unencoded, the ``#`` starts a fragment and the request silently goes to a different path.
    """
    return quote(value, safe=_PATH_SAFE)


def _resolve_version(version: Optional[str]) -> str:
    if not version:
        return DEFAULT_VERSION
    if version not in SUPPORTED_VERSIONS:
        raise GraphError(
            0, "invalidRequest", f"'version' must be one of {', '.join(SUPPORTED_VERSIONS)}"
        )
    return version


def allowlisted(headers: Mapping[str, str]) -> Dict[str, str]:
    """Only the named headers reach the caller."""
    lowered = {name.lower(): value for name, value in headers.items()}
    return {name: lowered[name.lower()] for name in _HEADER_ALLOWLIST if name.lower() in lowered}


def reject_authorization(headers: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Forward caller headers as supplied, except Authorization.

    This package owns authentication; a caller-supplied bearer token would silently bypass the
    credential the client was built with.
    """
    if not headers:
        return {}

    for name in headers:
        if name.lower() == "authorization":
            raise GraphError(
                0,
                "invalidRequest",
                "'Authorization' may not be supplied; the client owns authentication",
            )
    return dict(headers)
