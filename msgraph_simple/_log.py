"""Opt-in structured logging.

One JSON object per line on stderr, off unless ``MSGRAPH_LOG_LEVEL`` asks for it. Output goes to
stderr because a caller may be piping stdout.

Redaction is structural rather than a rule to remember: every function here takes the exact fields
it may emit, so there is no free-form call through which a header, a token or a request body could
reach a line. URLs are logged without their query string, because an OData ``$filter`` routinely
carries email addresses.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any, Dict, Iterator, Optional, TextIO
from urllib.parse import urlsplit

__all__ = ["LEVEL_VARIABLE", "capture", "is_enabled", "parse_level", "request", "event", "failure"]

LEVEL_VARIABLE = "MSGRAPH_LOG_LEVEL"

OFF, ERROR, INFO = 0, 1, 2
_NAMES = {ERROR: "error", INFO: "info"}


def parse_level(value: Optional[str]) -> int:
    """Anything unrecognised means off: a typo must not silently enable logging."""
    return {"error": ERROR, "info": INFO}.get((value or "").strip().lower(), OFF)


_configured = parse_level(os.environ.get(LEVEL_VARIABLE))

#: Test-only redirection. Production never sets it, so the production path holds no mutable state.
_override: Optional[tuple] = None


def _level() -> int:
    return _override[0] if _override else _configured


def _writer() -> TextIO:
    return _override[1] if _override else sys.stderr


def is_enabled(level: int) -> bool:
    return level <= _level()


@contextlib.contextmanager
def capture(level: int, writer: TextIO) -> Iterator[None]:
    """Capture output at the given level for the duration of the block. Tests only."""
    global _override
    previous, _override = _override, (level, writer)
    try:
        yield
    finally:
        _override = previous


def _write(level: int, fields: Dict[str, Any]) -> None:
    try:
        line = json.dumps({"level": _NAMES[level], **fields}, separators=(",", ":"))
        print(line, file=_writer(), flush=True)
    except Exception:
        # A diagnostic must never be the thing that takes the process down.
        pass


def _path_only(url: str) -> str:
    """Scheme, host and path. The query string is never logged."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def request(
    method: str,
    url: str,
    status: int,
    elapsed_ms: int,
    request_id: Optional[str] = None,
    error_code: Optional[str] = None,
) -> None:
    level = ERROR if error_code else INFO
    if not is_enabled(level):
        return

    _write(level, {
        "event": "request",
        "method": method,
        "url": _path_only(url),
        "status": status,
        "ms": elapsed_ms,
        "requestId": request_id,
        "errorCode": error_code,
    })


def event(name: str, **fields: Any) -> None:
    """A lifecycle event. Callers pass only scalars they have chosen to expose."""
    if is_enabled(INFO):
        _write(INFO, {"event": name, **fields})


def failure(operation: str, code: str, message: str) -> None:
    """A failure that produced no response. The message is what the caller already receives."""
    if is_enabled(ERROR):
        _write(ERROR, {"event": "failure", "operation": operation, "code": code, "message": message})
