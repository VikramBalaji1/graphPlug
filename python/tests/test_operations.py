"""Batching and file transfer.

Ported from the C# core's BatchOperationTests, FileTransferTests and FileRoundTripTests.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from _support import json_response, make_client

from msgraph_simple import GraphError
from msgraph_simple._operations import (
    CHUNK_ALIGNMENT, CHUNK_SIZE, CHUNKED_THRESHOLD_BYTES, MAX_BATCH_SIZE, to_session_path,
)


def _echo_batch(request: httpx.Request) -> httpx.Response:
    """Answer a $batch by echoing its sub-requests back, deliberately reversed."""
    sent = json.loads(request.content)["requests"]
    return json_response(200, {"responses": [
        {"id": item["id"], "status": 200, "body": {"url": item["url"]}}
        for item in reversed(sent)
    ]})


class Batching(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _requests(count: int):
        return [("GET", f"/users/{i}") for i in range(count)]

    async def test_a_25_request_batch_is_split_at_20(self) -> None:
        graph, rec, _ = make_client()
        rec.answer = _echo_batch

        async with graph:
            results = await graph.batch(self._requests(25))

        sizes = [len(json.loads(r.content)["requests"]) for r in rec.requests]
        self.assertEqual(sorted(sizes, reverse=True), [20, 5])
        self.assertEqual(len(results), 25)

    async def test_exactly_20_goes_as_one_chunk(self) -> None:
        graph, rec, _ = make_client()
        rec.answer = _echo_batch

        async with graph:
            await graph.batch(self._requests(20))

        self.assertEqual(len(rec.requests), 1)

    async def test_results_come_back_in_submission_order(self) -> None:
        # The transport reverses each chunk; submission order must be restored.
        graph, rec, _ = make_client()
        rec.answer = _echo_batch

        async with graph:
            results = await graph.batch(self._requests(25))

        self.assertEqual(
            [r["body"]["url"] for r in results],
            [f"/users/{i}" for i in range(25)],
        )

    async def test_a_failing_sub_request_is_reported_in_place(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            sent = json.loads(request.content)["requests"]
            return json_response(200, {"responses": [
                {
                    "id": item["id"],
                    "status": 404 if item["url"].endswith("/7") else 200,
                    "body": {"error": {"code": "itemNotFound"}} if item["url"].endswith("/7")
                            else {"url": item["url"]},
                }
                for item in sent
            ]})

        graph, rec, _ = make_client()
        rec.answer = answer

        async with graph:
            results = await graph.batch(self._requests(25))

        self.assertEqual(len(results), 25, "one failure must not discard the other 24")
        self.assertEqual(results[7]["status"], 404)
        self.assertEqual(results[8]["status"], 200)

    async def test_caller_supplied_ids_are_used_for_ordering(self) -> None:
        graph, rec, _ = make_client()
        rec.answer = _echo_batch

        async with graph:
            results = await graph.batch([
                {"id": "users", "method": "GET", "url": "/users"},
                {"id": "groups", "method": "GET", "url": "/groups"},
            ])

        self.assertEqual([r["id"] for r in results], ["users", "groups"])

    async def test_it_posts_to_the_batch_endpoint(self) -> None:
        graph, rec, _ = make_client()
        rec.answer = _echo_batch

        async with graph:
            await graph.batch(self._requests(1))

        self.assertTrue(str(rec.last.url).endswith("/v1.0/$batch"))
        self.assertEqual(rec.last.method, "POST")

    async def test_an_empty_batch_sends_nothing(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            self.assertEqual(await graph.batch([]), [])
        self.assertEqual(rec.requests, [])

    async def test_the_limit_matches_graphs(self) -> None:
        self.assertEqual(MAX_BATCH_SIZE, 20)


class UploadStrategy(unittest.TestCase):
    def test_the_threshold_is_exactly_4_mib(self) -> None:
        self.assertEqual(CHUNKED_THRESHOLD_BYTES, 4 * 1024 * 1024)

    def test_the_chunk_size_is_a_multiple_of_320_kib(self) -> None:
        # Graph rejects an upload session chunk that is not.
        self.assertEqual(CHUNK_ALIGNMENT, 320 * 1024)
        self.assertEqual(CHUNK_SIZE % CHUNK_ALIGNMENT, 0)
        self.assertEqual(CHUNK_SIZE, 10 * 1024 * 1024)

    def test_a_content_path_becomes_a_session_path(self) -> None:
        cases = [
            ("/me/drive/root:/big.zip:/content", "/me/drive/root:/big.zip:/createUploadSession"),
            ("/me/drive/items/01ABC/content", "/me/drive/items/01ABC/createUploadSession"),
            ("/me/drive/root:/b.zip:/createUploadSession", "/me/drive/root:/b.zip:/createUploadSession"),
        ]
        for given, expected in cases:
            with self.subTest(given=given):
                self.assertEqual(to_session_path(given), expected)


class Files(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="msgraph-files-"))

    def tearDown(self) -> None:
        for leftover in self.workspace.glob("*"):
            leftover.unlink(missing_ok=True)
        self.workspace.rmdir()

    async def test_a_download_streams_to_disk(self) -> None:
        destination = self.workspace / "out.bin"
        payload = b"x" * 5000

        graph, rec, _ = make_client()
        rec.answer = lambda request: httpx.Response(200, content=payload)

        async with graph:
            result = await graph.download("/me/drive/items/1/content", str(destination))

        self.assertEqual(result["bytesWritten"], 5000)
        self.assertEqual(destination.read_bytes(), payload)

    async def test_a_failed_download_leaves_nothing(self) -> None:
        destination = self.workspace / "never.bin"
        graph, _, _ = make_client(json_response(404, {"error": {"code": "itemNotFound"}}))

        async with graph:
            with self.assertRaises(GraphError):
                await graph.download("/me/drive/items/x/content", str(destination))

        self.assertFalse(destination.exists())
        self.assertEqual(list(self.workspace.glob("*.partial")), [])

    async def test_a_missing_destination_directory_is_reported_not_created(self) -> None:
        destination = self.workspace / "absent" / "out.bin"
        graph, _, _ = make_client(json_response(200, {}))

        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.download("/x/content", str(destination))

        self.assertIn("does not exist", raised.exception.message)
        self.assertFalse((self.workspace / "absent").exists())

    async def test_a_small_upload_is_a_single_put(self) -> None:
        source = self.workspace / "small.bin"
        source.write_bytes(os.urandom(1024))

        graph, rec, _ = make_client(json_response(201, {"id": "01ABC"}))
        async with graph:
            result = await graph.upload("/me/drive/root:/small.bin:/content", str(source))

        self.assertEqual(result["bytesSent"], 1024)
        self.assertEqual(len(rec.requests), 1)
        self.assertEqual(rec.last.method, "PUT")
        self.assertTrue(str(rec.last.url).endswith(":/content"))

    async def test_a_large_upload_uses_an_aligned_chunked_session(self) -> None:
        size = CHUNKED_THRESHOLD_BYTES * 2 + 12_345
        source = self.workspace / "big.bin"
        source.write_bytes(os.urandom(size))

        stored = bytearray(size)
        ranges = []

        def answer(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("createUploadSession"):
                return json_response(200, {"uploadUrl": "https://contoso.sharepoint.com/upload/s1"})
            content_range = request.headers["Content-Range"]
            span, total = content_range.split(" ", 1)[1].split("/")
            first, last = (int(v) for v in span.split("-"))
            ranges.append((first, last, int(total)))
            stored[first:last + 1] = request.content
            if last + 1 >= int(total):
                return json_response(201, {"id": "01ABC", "size": int(total)})
            return json_response(202, {"nextExpectedRanges": [f"{last + 1}-"]})

        graph, rec, _ = make_client()
        rec.answer = answer

        async with graph:
            result = await graph.upload("/me/drive/root:/big.bin:/content", str(source))

        self.assertEqual(result["bytesSent"], size)
        self.assertEqual(bytes(stored), source.read_bytes(), "the reassembled file differs")

        # Every chunk but the last is a whole number of alignment units, and they tile exactly.
        for first, last, total in ranges[:-1]:
            self.assertEqual((last - first + 1) % CHUNK_ALIGNMENT, 0)
            self.assertEqual(total, size)
        expected_start = 0
        for first, last, _ in ranges:
            self.assertEqual(first, expected_start)
            expected_start = last + 1
        self.assertEqual(expected_start, size)

    async def test_a_round_trip_preserves_the_checksum(self) -> None:
        size = CHUNKED_THRESHOLD_BYTES + 7
        source = self.workspace / "rt.bin"
        source.write_bytes(os.urandom(size))
        returned = self.workspace / "rt-back.bin"

        stored = bytearray(size)

        def answer(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("createUploadSession"):
                return json_response(200, {"uploadUrl": "https://contoso.sharepoint.com/upload/s1"})
            if request.method == "PUT":
                span = request.headers["Content-Range"].split(" ", 1)[1].split("/")[0]
                first, last = (int(v) for v in span.split("-"))
                stored[first:last + 1] = request.content
                if last + 1 >= size:
                    return json_response(201, {"id": "01ABC"})
                return json_response(202, {"nextExpectedRanges": [f"{last + 1}-"]})
            return httpx.Response(200, content=bytes(stored))

        graph, rec, _ = make_client()
        rec.answer = answer

        async with graph:
            await graph.upload("/me/drive/root:/rt.bin:/content", str(source))
            await graph.download("/me/drive/items/01ABC/content", str(returned))

        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
        self.assertEqual(digest(returned), digest(source))

    async def test_uploading_a_file_that_does_not_exist_is_rejected(self) -> None:
        graph, _, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.upload("/me/drive/root:/x:/content", str(self.workspace / "absent"))
        self.assertEqual(raised.exception.code, "invalidRequest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
