"""URL building, OData options, the verbs, paging and the error surface.

Ported from the C# core's GraphUrlBuilderTests, PaginationTests and ErrorMappingTests. Where an
assertion here differs from the C# one, the C# test is the verified behaviour and wins.
"""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from _support import json_response, make_client

from graphplug import GraphError
from graphplug._request import build_url, segment


class UrlBuilding(unittest.TestCase):
    def test_defaults_to_v1(self) -> None:
        self.assertEqual(build_url("/users"), "https://graph.microsoft.com/v1.0/users")

    def test_beta_is_opt_in(self) -> None:
        self.assertEqual(build_url("/users", "beta"), "https://graph.microsoft.com/beta/users")

    def test_a_leading_slash_is_added(self) -> None:
        self.assertEqual(build_url("users"), "https://graph.microsoft.com/v1.0/users")

    def test_an_unknown_version_is_rejected(self) -> None:
        with self.assertRaises(GraphError) as raised:
            build_url("/users", "v2.0")
        self.assertEqual(raised.exception.code, "invalidRequest")

    def test_a_missing_path_is_rejected(self) -> None:
        for path in (None, "", "   "):
            with self.subTest(path=path):
                with self.assertRaises(GraphError):
                    build_url(path)

    def test_an_absolute_url_passes_through_verbatim(self) -> None:
        link = "https://graph.microsoft.com/v1.0/users?$skiptoken=X-Y_Z"
        self.assertEqual(build_url(link, "beta", {"$top": 5}), link)

    def test_an_off_host_url_passes_through_for_downloads(self) -> None:
        url = "https://contoso.sharepoint.com/_layouts/download.aspx?id=1"
        self.assertEqual(build_url(url), url)

    def test_a_non_https_absolute_url_is_rejected(self) -> None:
        for url in ("http://graph.microsoft.com/v1.0/users", "ftp://example.com/x"):
            with self.subTest(url=url):
                with self.assertRaises(GraphError):
                    build_url(url)

    def test_a_rooted_path_is_relative_on_every_platform(self) -> None:
        # urlsplit("/users").scheme is empty, but a naive scheme check reads "C:\\x" as scheme "c".
        # The C# core shipped that bug and it broke every request on Linux while passing on Windows.
        for path in ("/users", "/me/drive/root:/big.zip:/content", "/sites/root/lists"):
            with self.subTest(path=path):
                self.assertEqual(build_url(path), f"https://graph.microsoft.com/v1.0{path}")

    def test_a_local_file_path_never_becomes_the_target(self) -> None:
        self.assertTrue(build_url("C:\\secrets").startswith("https://graph.microsoft.com/"))

    def test_odata_values_are_encoded(self) -> None:
        url = build_url("/users", None, {"$filter": "startsWith(displayName,'a b')"})
        self.assertIn("a+b", url.replace("%20", "+"))
        self.assertNotIn(" ", url)


class RequestConstruction(unittest.IsolatedAsyncioTestCase):
    async def test_odata_keywords_become_dollar_parameters(self) -> None:
        graph, rec, _ = make_client(json_response(200, {}))
        async with graph:
            await graph.get("/users", select="id,mail", filter="accountEnabled eq true", top=999)

        query = parse_qs(urlsplit(str(rec.last.url)).query)
        self.assertEqual(query["$select"], ["id,mail"])
        self.assertEqual(query["$filter"], ["accountEnabled eq true"])
        self.assertEqual(query["$top"], ["999"])

    async def test_unknown_options_pass_through_literally(self) -> None:
        graph, rec, _ = make_client(json_response(200, {}))
        async with graph:
            await graph.get("/users/delta", deltaToken="abc")

        self.assertIn("deltaToken=abc", str(rec.last.url))

    async def test_none_valued_options_are_omitted(self) -> None:
        graph, rec, _ = make_client(json_response(200, {}))
        async with graph:
            await graph.get("/users", select=None, top=5)

        query = parse_qs(urlsplit(str(rec.last.url)).query)
        self.assertNotIn("$select", query)

    async def test_the_verbs_map_and_return_the_body(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"m": "ok"}))
        async with graph:
            self.assertEqual(await graph.get("/x"), {"m": "ok"})
            self.assertEqual(await graph.post("/x", body={}), {"m": "ok"})
            self.assertEqual(await graph.patch("/x", body={}), {"m": "ok"})
            await graph.delete("/x")

        self.assertEqual([r.method for r in rec.requests], ["GET", "POST", "PATCH", "DELETE"])

    async def test_request_returns_the_whole_envelope(self) -> None:
        graph, _, _ = make_client(json_response(200, {"value": []}, **{"request-id": "rid"}))
        async with graph:
            envelope = await graph.request("GET", "/users")

        self.assertEqual(envelope["status"], 200)
        self.assertEqual(envelope["headers"]["request-id"], "rid")

    async def test_beta_is_reachable_per_call(self) -> None:
        graph, rec, _ = make_client(json_response(200, {}))
        async with graph:
            await graph.request("GET", "/users", version="beta")

        self.assertIn("/beta/users", str(rec.last.url))


class Pagination(unittest.IsolatedAsyncioTestCase):
    async def test_it_walks_every_page(self) -> None:
        graph, rec, _ = make_client()
        pages = [
            json_response(200, {"value": [{"id": "1"}, {"id": "2"}],
                                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?p=2"}),
            json_response(200, {"value": [{"id": "3"}],
                                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?p=3"}),
            json_response(200, {"value": [{"id": "4"}]}),
        ]
        served = iter(pages)
        rec.answer = lambda request: next(served)

        async with graph:
            found = [item["id"] async for item in graph.paged("/users", select="id")]

        self.assertEqual(found, ["1", "2", "3", "4"])
        self.assertEqual(len(rec.requests), 3)

    async def test_the_next_link_is_echoed_back_without_the_original_query(self) -> None:
        graph, rec, _ = make_client()
        pages = [
            json_response(200, {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}),
            json_response(200, {"value": []}),
        ]
        served = iter(pages)
        rec.answer = lambda request: next(served)

        async with graph:
            [item async for item in graph.paged("/users", select="id,mail")]

        self.assertEqual(str(rec.requests[1].url), "https://graph.microsoft.com/v1.0/next")

    async def test_an_empty_page_yields_nothing(self) -> None:
        graph, _, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            self.assertEqual([x async for x in graph.paged("/users")], [])

    async def test_abandoning_the_generator_fetches_no_more(self) -> None:
        graph, rec, _ = make_client(json_response(
            200, {"value": [{"id": "1"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/n"}
        ))
        async with graph:
            async for item in graph.paged("/users"):
                self.assertEqual(item["id"], "1")
                break

        self.assertEqual(len(rec.requests), 1)

    async def test_a_204_has_no_body(self) -> None:
        graph, _, _ = make_client(json_response(204))
        async with graph:
            envelope = await graph.request("DELETE", "/users/1")

        self.assertEqual(envelope["status"], 204)
        self.assertIsNone(envelope["body"])


class Errors(unittest.IsolatedAsyncioTestCase):
    THROTTLED = {
        "error": {
            "code": "activityLimitReached",
            "message": "Too many requests.",
            "innerError": {"code": "quotaLimitReached", "request-id": "a1b2c3d4"},
        }
    }

    async def test_a_graph_error_becomes_a_graph_error(self) -> None:
        graph, _, _ = make_client(json_response(
            404, {"error": {"code": "itemNotFound", "message": "not found"}},
            **{"request-id": "rid-9"},
        ))
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.get("/users/nope")

        error = raised.exception
        self.assertEqual(error.status, 404)
        self.assertEqual(error.code, "itemNotFound")
        self.assertEqual(error.message, "not found")
        self.assertEqual(error.request_id, "rid-9")

    async def test_the_inner_error_is_preserved(self) -> None:
        # The retry budget must be exhausted for the 429 to surface, so script four of them.
        graph, _, _ = make_client(*[
            json_response(429, self.THROTTLED, **{"Retry-After": "0"}) for _ in range(5)
        ])
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.get("/users")

        self.assertEqual(raised.exception.code, "activityLimitReached")
        self.assertEqual(raised.exception.inner, {"code": "quotaLimitReached", "request-id": "a1b2c3d4"})

    async def test_a_non_json_body_falls_back_to_the_status(self) -> None:
        import httpx
        graph, rec, _ = make_client()
        rec.answer = lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")

        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.get("/users")

        self.assertEqual(raised.exception.status, 502)
        self.assertIn("Bad Gateway", raised.exception.message)

    async def test_the_message_carries_status_and_code(self) -> None:
        graph, _, _ = make_client(json_response(403, {"error": {"code": "accessDenied", "message": "no"}}))
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.get("/users")

        self.assertEqual(str(raised.exception), "[403 accessDenied] no")


class Encoding(unittest.TestCase):
    def test_a_boolean_goes_on_the_wire_in_odata_case(self) -> None:
        self.assertTrue(build_url("/users", None, {"$count": True}).endswith("?$count=true"))

    def test_options_join_a_query_the_path_already_has(self) -> None:
        self.assertEqual(build_url("/me/messages?$top=5", None, {"$select": "id"}),
                         "https://graph.microsoft.com/v1.0/me/messages?$top=5&$select=id")

    def test_a_url_inside_a_path_does_not_make_it_absolute(self) -> None:
        path = "/me/drive/root/search(q='http://intranet')"
        self.assertEqual(build_url(path), f"https://graph.microsoft.com/v1.0{path}")

    def test_segment_encodes_what_would_end_the_path(self) -> None:
        self.assertEqual(segment("bob_x.com#EXT#@t.onmicrosoft.com"),
                         "bob_x.com%23EXT%23@t.onmicrosoft.com")
        self.assertEqual(segment("/Q3 #1?.xlsx"), "/Q3%20%231%3F.xlsx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
