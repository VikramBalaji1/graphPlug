"""Paging, batching and file transfer.

Ported from the C# core's `BatchOperation`, `DownloadOperation` and the two upload strategies.
None of msgraph-core's own helpers can be used for these: `BatchRequestBuilder`, `PageIterator` and
`LargeFileUploadTask` each require a Kiota `RequestAdapter` and `Parsable` models, which this
package deliberately does not build.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ._errors import GraphError
from ._http import Transport
from ._request import build_url

__all__ = [
    "paged", "batch", "download", "upload",
    "MAX_BATCH_SIZE", "CHUNKED_THRESHOLD_BYTES", "CHUNK_SIZE", "CHUNK_ALIGNMENT",
]

#: Graph's hard limit on requests per batch.
MAX_BATCH_SIZE = 20

#: Below this a single PUT is fine; at or above it Graph wants an upload session.
CHUNKED_THRESHOLD_BYTES = 4 * 1024 * 1024

#: Graph requires an upload session's chunk size to be a multiple of 320 KiB.
CHUNK_ALIGNMENT = 320 * 1024
CHUNK_SIZE = 10 * 1024 * 1024  # 32 whole alignment units

_COPY_BUFFER = 1 << 16
_MAX_CHUNK_ATTEMPTS = 3

BatchRequest = Union[Tuple[str, str], Mapping[str, Any]]


# ── paging ───────────────────────────────────────────────────────────────────


async def paged(transport: Transport, url: str) -> AsyncIterator[Dict[str, Any]]:
    """Walk every page, yielding items.

    Nothing buffers the whole collection, and abandoning the generator leaks nothing: the next
    link is a complete, self-describing cursor held only by the caller's loop.
    """
    while url:
        envelope = await transport.json("GET", url, operation="paged")
        body = envelope.get("body") or {}

        for item in body.get("value", []):
            yield item

        url = envelope.get("nextLink") or ""


# ── batching ─────────────────────────────────────────────────────────────────


def _prepare(requests: Sequence[BatchRequest]) -> List[Dict[str, Any]]:
    """Normalise to Graph's sub-request shape, assigning ids so ordering has a key."""
    prepared: List[Dict[str, Any]] = []
    for index, request in enumerate(requests):
        if isinstance(request, tuple):
            method, url = request
            item: Dict[str, Any] = {"method": method, "url": url}
        else:
            item = dict(request)
        item.setdefault("id", str(index))
        prepared.append(item)
    return prepared


async def batch(
    transport: Transport,
    requests: Sequence[BatchRequest],
    version: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Send many requests as one call.

    Two pieces of behaviour belong here rather than to the caller. Graph rejects batches larger
    than 20, so a bigger set is split; and Graph does not guarantee response order within a batch,
    so submission order is restored and results align positionally with what was sent.

    Per-request failures are not raised -- each result keeps its own ``status``. One failing
    sub-request must not discard nineteen successful ones.
    """
    prepared = _prepare(requests)
    if not prepared:
        return []

    chunks = [prepared[i:i + MAX_BATCH_SIZE] for i in range(0, len(prepared), MAX_BATCH_SIZE)]
    url = build_url("/$batch", version)

    # Chunks go concurrently; the transport's semaphore is what keeps that within Graph's limits.
    responses = await asyncio.gather(*(
        transport.json("POST", url, body={"requests": chunk}, operation="batch")
        for chunk in chunks
    ))

    merged: List[Dict[str, Any]] = []
    for chunk, envelope in zip(chunks, responses):
        merged.extend(_ordered(chunk, (envelope.get("body") or {}).get("responses", [])))
    return merged


def _ordered(sent: Sequence[Mapping[str, Any]], returned: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Restore submission order. Graph returns sub-responses in whatever order they completed."""
    by_id = {str(item.get("id")): dict(item) for item in returned if item.get("id") is not None}
    return [by_id[str(item["id"])] for item in sent if str(item["id"]) in by_id]


# ── download ─────────────────────────────────────────────────────────────────


async def download(transport: Transport, url: str, dest_path: str) -> Dict[str, Any]:
    """Stream a response straight to disk.

    The bytes never enter a JSON envelope and never fully enter memory, so size is bounded by disk
    rather than RAM. The destination directory must already exist; this does not create it.
    """
    destination = Path(dest_path)
    if destination.parent and not destination.parent.exists():
        raise GraphError(
            0, "invalidRequest", f"the destination directory '{destination.parent}' does not exist"
        )

    response = await transport.send("GET", url, stream=True)
    try:
        if not response.is_success:
            await response.aread()
            return Transport.envelope(response)  # raises with the Graph error

        # Written to a temporary sibling and renamed on success, so a failed or cancelled
        # download cannot leave a truncated file at the destination path.
        partial = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.partial")
        written = 0
        try:
            with partial.open("wb") as handle:
                async for block in response.aiter_bytes(_COPY_BUFFER):
                    handle.write(block)
                    written += len(block)
            os.replace(partial, destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    finally:
        await response.aclose()

    from ._request import allowlisted
    return {
        "status": response.status_code,
        "headers": allowlisted(response.headers),
        "bytesWritten": written,
        "destPath": str(destination),
    }


# ── upload ───────────────────────────────────────────────────────────────────

_CONTENT_SUFFIX = ":/content"
_SESSION_SUFFIX = ":/createUploadSession"


def to_session_path(path: str) -> str:
    """Turn the content path the caller wrote into the session path Graph expects.

    Both strategies take the same input, so the caller never learns which one ran.
    """
    if path.endswith(_CONTENT_SUFFIX):
        return path[: -len(_CONTENT_SUFFIX)] + _SESSION_SUFFIX
    if path.endswith("/content"):
        return path[: -len("/content")] + "/createUploadSession"
    return path


async def upload(
    transport: Transport,
    path: str,
    source_path: str,
    version: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a local file, choosing the strategy by size."""
    source = Path(source_path)
    if not source.is_file():
        raise GraphError(0, "invalidRequest", f"'{source_path}' does not exist")

    size = source.stat().st_size
    if size < CHUNKED_THRESHOLD_BYTES:
        envelope = await _upload_simple(transport, path, source, version)
    else:
        envelope = await _upload_chunked(transport, path, source, size, version)

    envelope["bytesSent"] = size
    return envelope


async def _upload_simple(
    transport: Transport, path: str, source: Path, version: Optional[str]
) -> Dict[str, Any]:
    """A single PUT of the file. Used below 4 MiB."""
    response = await transport.send(
        "PUT",
        build_url(path, version),
        headers={"Content-Type": "application/octet-stream"},
        content=source.read_bytes(),
    )
    return Transport.envelope(response)


async def _upload_chunked(
    transport: Transport, path: str, source: Path, size: int, version: Optional[str]
) -> Dict[str, Any]:
    """createUploadSession followed by sequential ranged PUTs. Used at 4 MiB and above."""
    created = await transport.json(
        "POST", build_url(to_session_path(path), version), operation="upload"
    )
    upload_url = (created.get("body") or {}).get("uploadUrl")
    if not upload_url:
        raise GraphError(0, "internalError", "the upload session response carried no uploadUrl")

    offset = 0
    last = None
    with source.open("rb") as handle:
        while offset < size:
            handle.seek(offset)
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break

            last = await _put_chunk(transport, upload_url, chunk, offset, size)
            if not last.is_success and last.status_code not in (200, 201, 202):
                return Transport.envelope(last)  # raises with the Graph error

            offset += len(chunk)

            # The service's own account of progress wins over ours.
            resume = _next_expected(last)
            if resume is not None and resume != offset and resume < size:
                offset = resume

    if last is None:
        raise GraphError(0, "invalidRequest", "the file is empty")
    return Transport.envelope(last)


async def _put_chunk(transport: Transport, upload_url: str, chunk: bytes, offset: int, total: int):
    """Send one chunk, retrying it against a transient failure."""
    response = None
    for _ in range(_MAX_CHUNK_ATTEMPTS):
        response = await transport.send(
            "PUT",
            upload_url,
            headers={
                "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{total}",
                "Content-Type": "application/octet-stream",
            },
            content=chunk,
        )
        if response.is_success or response.status_code not in (408, 429, 500, 502, 503, 504):
            return response
    return response


def _next_expected(response) -> Optional[int]:
    """Read the session's nextExpectedRanges, which is what makes a large upload resumable."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None

    ranges = body.get("nextExpectedRanges")
    if not ranges:
        return None
    try:
        return int(str(ranges[0]).split("-", 1)[0])
    except (ValueError, IndexError):
        return None
