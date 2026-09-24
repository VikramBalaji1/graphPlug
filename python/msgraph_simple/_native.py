"""The ctypes layer (ARCHITECTURE.md 11.4).

Everything that touches a raw pointer lives here. The rest of the package sees dictionaries.
"""

from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

__all__ = ["GraphError", "call", "core"]

#: Must match ``Exports.CoreVersion`` in the C# core. The shared library and this file ship as one
#: unit, so a mismatched pair fails loudly rather than subtly (6.6).
WHEEL_VERSION = "0.1.0"

#: NativeAOT names the output after the assembly and adds no "lib" prefix, so this is
#: MicrosoftGraph.so rather than the libMicrosoftGraph.so a Linux shared library usually is.
#: Nothing dlopens it by bare name -- it is always loaded by full path -- so the produced name
#: is used as-is rather than renamed to satisfy a convention that buys nothing here.
_LIBRARY_NAMES = {
    "linux": "MicrosoftGraph.so",
    "darwin": "MicrosoftGraph.dylib",
    "win32": "MicrosoftGraph.dll",
}

_INT64 = ctypes.c_int64
_STR = ctypes.c_char_p

#: restype is ``c_void_p``, never ``c_char_p``. ctypes auto-converts ``c_char_p`` to ``bytes`` and
#: discards the pointer, which would make ``graph_free`` impossible and leak every response.
_SIGNATURES: Dict[str, Any] = {
    "graph_client_create": ([_STR], ctypes.c_void_p),
    "graph_auth_begin": ([_STR], ctypes.c_void_p),
    "graph_auth_complete": ([_INT64, _STR], ctypes.c_void_p),
    "graph_auth_cancel": ([_INT64], ctypes.c_void_p),
    "graph_client_close": ([_INT64], ctypes.c_void_p),
    "graph_request": ([_INT64, _STR], ctypes.c_void_p),
    "graph_download": ([_INT64, _STR], ctypes.c_void_p),
    "graph_upload": ([_INT64, _STR], ctypes.c_void_p),
    "graph_free": ([ctypes.c_void_p], None),
}


class GraphError(Exception):
    """Every failure, in one shape (9.3).

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
        self.status = status
        self.code = code
        self.message = message
        #: Quote this to Microsoft support.
        self.request_id = request_id
        self.retry_after = retry_after
        self.inner = inner

    @classmethod
    def from_envelope(cls, envelope: Dict[str, Any]) -> "GraphError":
        error = envelope.get("error") or {}
        return cls(
            status=int(envelope.get("status", 0)),
            code=str(error.get("code", "internalError")),
            message=str(error.get("message", "the core reported a failure with no message")),
            request_id=error.get("requestId"),
            retry_after=error.get("retryAfterSeconds"),
            inner=error.get("innerError"),
        )


def _library_path(lib_dir: Optional[Path] = None) -> Path:
    """Locate the bundled library. ``lib_dir`` is a test seam, not a supported override."""
    name = _LIBRARY_NAMES.get(sys.platform)
    if name is None:
        raise GraphError(0, "unsupportedPlatform", f"no native library for {sys.platform!r}")

    path = (lib_dir or Path(__file__).resolve().parent / "_lib") / name
    if not path.is_file():
        raise GraphError(
            0,
            "libraryNotFound",
            f"the native library is missing from the package at {path}",
        )
    return path


def _load() -> ctypes.CDLL:
    library = ctypes.CDLL(str(_library_path()))
    for name, (argtypes, restype) in _SIGNATURES.items():
        function = getattr(library, name)
        # Omitting these is the classic way to corrupt pointers on 64-bit platforms.
        function.argtypes = argtypes
        function.restype = restype

    _verify_core_version(library)
    return library


def _verify_core_version(library: ctypes.CDLL) -> None:
    """Check the pair before anything real runs (6.6).

    Every envelope reports ``coreVersion``, including a rejected one, so an empty credentials
    envelope is enough: the core parses nothing, creates no session and touches no network, and
    the error it returns still carries the version. A mismatch here is a packaging fault, and it
    is far better caught at import than as a strange failure three calls later.
    """
    pointer = library.graph_client_create(b"")
    if not pointer:
        raise GraphError(0, "nullPointer", "the core returned no envelope during version check")

    try:
        envelope = json.loads(ctypes.string_at(pointer).decode("utf-8"))
    finally:
        library.graph_free(pointer)

    reported = envelope.get("coreVersion")
    if reported is None:
        raise GraphError(
            0,
            "coreVersionMismatch",
            "the native library reported no version; it predates this package",
        )

    _check_core_version(envelope)


_library: Optional[ctypes.CDLL] = None


def core() -> ctypes.CDLL:
    """The loaded library, opened on first use so importing the package cannot fail."""
    global _library
    if _library is None:
        _library = _load()
    return _library


def call(name: str, *args: Any) -> Dict[str, Any]:
    """Invoke one export, decode its envelope, and always release the buffer.

    Raises :class:`GraphError` when the core reports a failure, so callers never check ``ok``.
    """
    library = core()
    encoded = [arg.encode("utf-8") if isinstance(arg, str) else arg for arg in args]

    pointer = getattr(library, name)(*encoded)
    if not pointer:
        raise GraphError(0, "nullPointer", f"{name} returned no envelope")

    try:
        envelope = json.loads(ctypes.string_at(pointer).decode("utf-8"))
    finally:
        # graph_free(NULL) is a no-op, so this is safe even on a partial failure above.
        library.graph_free(pointer)

    if not envelope.get("ok", False):
        raise GraphError.from_envelope(envelope)

    _check_core_version(envelope)
    return envelope


def _check_core_version(envelope: Dict[str, Any]) -> None:
    """The library and this file ship together; a mismatched pair must fail loudly (6.6)."""
    reported = envelope.get("coreVersion")
    if reported is not None and reported != WHEEL_VERSION:
        raise GraphError(
            0,
            "coreVersionMismatch",
            f"the native core reports {reported!r} but this package is {WHEEL_VERSION!r}; "
            "they ship as one unit and must match",
        )


def dumps(value: Any) -> str:
    """Compact JSON for the outbound side of the boundary."""
    return json.dumps(value, separators=(",", ":"))


_Marshaller = Callable[..., Dict[str, Any]]
