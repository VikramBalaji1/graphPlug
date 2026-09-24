"""The transport: middleware, auth attachment, header allowlisting and concurrency.

The first test here is the most important in the suite. msgraph-core runs its middleware only when
a request carries an ``options`` attribute, which is normally set by Kiota's RequestAdapter. This
package sets it by hand. If that undocumented contract ever changes, retry, Retry-After, redirect
and telemetry stop working silently -- requests simply go straight to the socket and nothing looks
wrong until Graph throttles you in production. This test is what makes that loud.
"""

from __future__ import annotations

import asyncio
import time
import unittest

import httpx

from _support import FakeCredential, Recorder, json_response, make_client

from msgraph_simple import GraphError


class MiddlewareContract(unittest.IsolatedAsyncioTestCase):
    async def test_a_429_is_retried_and_retry_after_is_honoured(self) -> None:
        graph, rec, _ = make_client(
            json_response(429, {"error": {"code": "activityLimitReached"}}, **{"Retry-After": "1"}),
            json_response(200, {"value": []}),
        )

        started = time.monotonic()
        async with graph:
            body = await graph.get("/users")
        elapsed = time.monotonic() - started

        self.assertEqual(body, {"value": []})
        self.assertEqual(len(rec.requests), 2, "the retry handler did not fire")
        self.assertGreaterEqual(elapsed, 1.0, "Retry-After was not honoured")

    async def test_the_middleware_pipeline_actually_runs(self) -> None:
        # The user agent is proof the pipeline engaged: plain httpx does not add kiota's.
        graph, rec, _ = make_client(json_response(200, {}))
        async with graph:
            await graph.get("/me")

        self.assertIn("kiota", (rec.header("user-agent") or "").lower())

    async def test_a_503_is_retried_too(self) -> None:
        graph, rec, _ = make_client(
            json_response(503, {"error": {"code": "serviceNotAvailable"}}),
            json_response(200, {"ok": True}),
        )
        async with graph:
            await graph.get("/users")

        self.assertEqual(len(rec.requests), 2)

    async def test_a_404_is_not_retried(self) -> None:
        graph, rec, _ = make_client(json_response(404, {"error": {"code": "itemNotFound"}}))
        async with graph:
            with self.assertRaises(GraphError):
                await graph.get("/users/nope")

        self.assertEqual(len(rec.requests), 1)


class Authorization(unittest.IsolatedAsyncioTestCase):
    async def test_the_bearer_token_is_attached_to_graph_requests(self) -> None:
        graph, rec, cred = make_client(json_response(200, {}))
        async with graph:
            await graph.get("/me")

        self.assertEqual(rec.header("authorization"), f"Bearer {FakeCredential.TOKEN}")
        self.assertEqual(cred.acquisitions, 1)

    async def test_the_token_is_withheld_from_an_off_host_url(self) -> None:
        # A pre-authenticated download URL needs no token and must not be handed one.
        graph, rec, cred = make_client(json_response(200, {}))
        async with graph:
            await graph.get("https://contoso.sharepoint.com/_layouts/download.aspx?id=1")

        self.assertIsNone(rec.header("authorization"))
        self.assertEqual(cred.acquisitions, 0, "a token was fetched for another host")

    async def test_a_caller_supplied_authorization_header_is_rejected(self) -> None:
        graph, rec, _ = make_client(json_response(200, {}))
        async with graph:
            for name in ("Authorization", "authorization", "AUTHORIZATION"):
                with self.subTest(name=name):
                    with self.assertRaises(GraphError) as raised:
                        await graph.request("GET", "/users", headers={name: "Bearer leaked"})
                    self.assertEqual(raised.exception.code, "invalidRequest")

        self.assertEqual(rec.requests, [], "the request must never reach the wire")


class ResponseHeaders(unittest.IsolatedAsyncioTestCase):
    async def test_only_allowlisted_headers_are_returned(self) -> None:
        graph, _, _ = make_client(json_response(
            200, {},
            **{
                "request-id": "rid-1",
                "client-request-id": "crid-1",
                "ETag": 'W/"1"',
                "Authorization": "Bearer should-never-escape",
                "WWW-Authenticate": 'Bearer realm="graph"',
                "x-ms-ags-diagnostic": "internal",
            },
        ))
        async with graph:
            envelope = await graph.request("GET", "/users")

        headers = envelope["headers"]
        self.assertIn("request-id", headers)
        self.assertIn("ETag", headers)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertNotIn("x-ms-ags-diagnostic", headers)

    async def test_no_credential_material_reaches_the_caller(self) -> None:
        graph, _, _ = make_client(json_response(
            401,
            {"error": {"code": "InvalidAuthenticationToken", "message": "Access token is empty."}},
            **{"Authorization": "Bearer super-secret", "WWW-Authenticate": 'Bearer realm=""'},
        ))
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.get("/me")

        rendered = repr(raised.exception.__dict__)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("WWW-Authenticate", rendered)


class Concurrency(unittest.IsolatedAsyncioTestCase):
    async def test_in_flight_requests_never_exceed_the_limit(self) -> None:
        graph, rec, _ = make_client(max_concurrency=4)

        async def slow(request: httpx.Request) -> httpx.Response:
            return json_response(200, {"ok": True})

        rec.answer = lambda request: json_response(200, {"ok": True})

        async with graph:
            await asyncio.gather(*(graph.get(f"/users/{i}") for i in range(40)))

        self.assertEqual(len(rec.requests), 40)
        self.assertLessEqual(rec.peak_in_flight, 4, "the semaphore did not bound concurrency")

    async def test_the_default_limit_is_modest(self) -> None:
        from msgraph_simple._http import DEFAULT_MAX_CONCURRENCY
        # Graph throttles per app and per tenant; going wide earns 429s rather than throughput.
        self.assertLessEqual(DEFAULT_MAX_CONCURRENCY, 16)


class Lifetime(unittest.IsolatedAsyncioTestCase):
    async def test_the_context_manager_closes(self) -> None:
        graph, _, _ = make_client(json_response(200, {}))
        async with graph:
            await graph.get("/me")

        with self.assertRaises(GraphError) as raised:
            await graph.get("/me")
        self.assertEqual(raised.exception.code, "invalidHandle")

    async def test_closing_twice_is_harmless(self) -> None:
        graph, _, _ = make_client()
        await graph.aclose()
        await graph.aclose()

    async def test_it_closes_even_when_the_body_raises(self) -> None:
        graph, _, _ = make_client()
        with self.assertRaises(ValueError):
            async with graph:
                raise ValueError("boom")

        with self.assertRaises(GraphError):
            await graph.get("/me")


if __name__ == "__main__":
    unittest.main(verbosity=2)
