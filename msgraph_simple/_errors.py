"""One error shape for every failure.

Ported from the C# core's `GraphErrorInfo`, `EntraFailure` and `ErrorEnvelope`. The codes are the
same strings, deliberately: they are what callers branch on and what the documentation promises.
"""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

__all__ = ["GraphError", "from_response", "code_for_exception", "code_for_text", "flatten"]

_MAX_BODY_CHARS = 2048
_MAX_CHAIN_DEPTH = 8


class GraphError(Exception):
    """Every failure, in one shape.

    One exception type rather than a hierarchy: callers branch on ``status`` and ``code``, which is
    more precise than any class tree and does not require importing eight names to write an
    ``except`` clause.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: Optional[str] = None,
        retry_after: Optional[int] = None,
        inner: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"[{status} {code}] {message}")
        #: HTTP status, or 0 when there was no HTTP response at all.
        self.status = status
        #: A Graph error code, or one of the core codes below.
        self.code = code
        self.message = message
        #: Quote this to Microsoft support.
        self.request_id = request_id
        self.retry_after = retry_after
        #: Graph's own inner error, preserved verbatim -- often the only actionable part.
        self.inner = inner


def parse_retry_after(value: Optional[str]) -> Optional[int]:
    """Accepts both forms RFC 9110 allows: delta-seconds and an HTTP date."""
    if not value:
        return None

    try:
        return max(0, int(value))
    except ValueError:
        pass

    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        # Raises rather than returning None on Python 3.10+. This runs while already handling a
        # failure, so a malformed header must not become a second, worse one.
        return None

    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


def from_response(
    status: int,
    headers: Mapping[str, str],
    body: Any,
    reason: Optional[str] = None,
) -> GraphError:
    """Build the error for a request that reached Graph.

    ``headers`` is already allow-listed by the caller, so nothing sensitive can arrive here.
    """
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {}

    fallback = body if isinstance(body, str) else ""
    return GraphError(
        status=status,
        code=str(error.get("code") or reason or status),
        message=str(error.get("message") or fallback[:_MAX_BODY_CHARS] or reason or str(status)),
        request_id=headers.get("request-id"),
        retry_after=parse_retry_after(headers.get("Retry-After")),
        inner=error.get("innerError") if isinstance(error.get("innerError"), dict) else None,
    )


def flatten(exception: BaseException) -> str:
    """Every message in the exception chain.

    The detail is never on the outermost exception: azure-identity raises with a message like
    "DeviceCodeCredential authentication failed: " and puts the AADSTS number underneath. Reading
    only the top message silently collapses every sign-in failure into one generic code.
    """
    messages, seen, current, depth = [], set(), exception, 0
    while current is not None and depth < _MAX_CHAIN_DEPTH and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        if text and text not in messages:
            messages.append(text)
        current = current.__cause__ or current.__context__
        depth += 1

    return " -- ".join(messages) or type(exception).__name__


def code_for_text(text: str) -> Optional[str]:
    """Match an AADSTS number or OAuth error string, or return None.

    Matched on the numbers and machine-readable strings rather than on prose, which is localised
    and reworded over time. Split out from ``code_for_exception`` because the MSAL token exchange
    reports a failure as a string rather than as an exception, and wrapping that string in a
    GraphError just to classify it made the classifier return the wrapper's own placeholder code.
    """
    # 65004 is an active refusal; 65001 is merely the absence of a consent grant.
    if "AADSTS65004" in text:
        return "signInDeclined"
    if "AADSTS65001" in text or "consent_required" in text:
        return "consentRequired"
    if "AADSTS70016" in text or "expired_token" in text or "code_expired" in text:
        return "signInTimeout"
    if "authorization_declined" in text or "access_denied" in text:
        return "signInDeclined"
    return None


def code_for_exception(exception: BaseException) -> str:
    """Map a non-HTTP failure onto one of the defined codes."""
    if isinstance(exception, GraphError):
        return exception.code

    text = flatten(exception)
    matched = code_for_text(text)
    if matched:
        return matched

    name = type(exception).__name__
    if name in ("TimeoutException", "ConnectTimeout", "ReadTimeout", "PoolTimeout", "TimeoutError"):
        return "timeout"
    if name in ("ConnectError", "ReadError", "WriteError", "NetworkError", "TransportError"):
        return "transportError"
    if "Authentication" in name or "Credential" in name or "AADSTS" in text:
        return "authenticationFailed"
    if isinstance(exception, (ValueError, TypeError, KeyError)):
        return "invalidRequest"
    return "internalError"


def as_graph_error(exception: BaseException, operation: str) -> GraphError:
    """Turn any exception into the one shape, and say so in the log."""
    if isinstance(exception, GraphError):
        return exception

    code = code_for_exception(exception)
    message = flatten(exception)

    from . import _log
    _log.failure(operation, code, message)
    return GraphError(0, code, message)
