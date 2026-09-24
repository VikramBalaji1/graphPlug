"""Upload a file, download it back, and send several requests as one.

Shows the three things the core does that are tedious to do by hand: switching to a chunked
upload session above 4 MiB, streaming a download to disk without buffering it, and splitting a
batch at Graph's limit of 20 while keeping your ordering.

    Needs:  Files.ReadWrite and User.Read as DELEGATED permissions
    Run:    export AZURE_TENANT_ID=... AZURE_CLIENT_ID=...
            python samples/files_and_batching.py
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

from msgraph_simple import GraphClient, GraphError

SIZE_MB = 6  # over the 4 MiB threshold, so the chunked path runs


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    tenant = os.environ.get("AZURE_TENANT_ID")
    client = os.environ.get("AZURE_CLIENT_ID")
    if not tenant or not client:
        print("set AZURE_TENANT_ID and AZURE_CLIENT_ID", file=sys.stderr)
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="msgraph-sample-"))
    source = workspace / "sample.bin"
    source.write_bytes(os.urandom(SIZE_MB * 1024 * 1024))

    try:
        with GraphClient.device_code(tenant, client, ["User.Read", "Files.ReadWrite"]) as graph:

            # ── upload ───────────────────────────────────────────────────────
            # Above 4 MiB this becomes createUploadSession plus ranged PUTs, on its own. You
            # write the same call either way and never learn which path ran.
            print(f"uploading {SIZE_MB} MB ...")
            uploaded = graph.upload("/me/drive/root:/sample.bin:/content", str(source))
            item_id = uploaded["body"]["id"]
            print(f"  sent {uploaded['bytesSent']:,} bytes, item {item_id}")

            # ── download ─────────────────────────────────────────────────────
            # Streams straight to disk. The bytes never enter a JSON envelope and never fully
            # enter memory, so size is bounded by disk rather than RAM. The directory must
            # already exist -- the core will not create it.
            returned = workspace / "returned.bin"
            result = graph.download(f"/me/drive/items/{item_id}/content", str(returned))
            print(f"  got back {result['bytesWritten']:,} bytes")

            match = sha256(source) == sha256(returned)
            print(f"  checksums {'match' if match else 'DIFFER'}")

            # ── batching ─────────────────────────────────────────────────────
            # More than 20 would be split into chunks and merged back in this order.
            print("\nbatching three requests as one call ...")
            results = graph.batch([
                ("GET", "/me"),
                ("GET", "/me/drive/root/children?$top=3"),
                ("GET", "/me/nope-this-one-fails"),
            ])

            # A failing sub-request is reported in place, never raised. One failure must not
            # discard the others, so check each status yourself.
            for index, sub in enumerate(results):
                status = sub["status"]
                mark = "ok " if 200 <= status < 300 else "ERR"
                print(f"  [{mark}] request {index}: {status}")

            graph.delete(f"/me/drive/items/{item_id}")
            print("\ncleaned up.")
        return 0

    except GraphError as error:
        print(f"failed: [{error.status} {error.code}] {error.message}", file=sys.stderr)
        if error.retry_after:
            print(f"throttled; retry in {error.retry_after}s", file=sys.stderr)
        return 1
    finally:
        for leftover in workspace.glob("*"):
            leftover.unlink(missing_ok=True)
        workspace.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
