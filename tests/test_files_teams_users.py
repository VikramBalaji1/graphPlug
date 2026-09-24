"""Files, Teams and Users: the paths and payloads they build.

Same standard as the mail and calendar tests. The whole value of these classes is getting Graph's
addressing and its nested JSON right, so the assertions are against what goes on the wire.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from _support import json_response, make_client

from msgraph_simple import GraphError


def query_of(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


def path_of(request: httpx.Request) -> str:
    return unquote(urlsplit(str(request.url)).path)


# ── users ────────────────────────────────────────────────────────────────────


class UserLookups(unittest.IsolatedAsyncioTestCase):
    async def test_me_asks_for_the_signed_in_person(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"displayName": "Alice"}))
        async with graph:
            person = await graph.users.me()

        self.assertEqual(path_of(rec.last), "/v1.0/me")
        self.assertEqual(person["displayName"], "Alice")

    async def test_find_sends_the_consistency_header_and_searches_both_fields(self) -> None:
        # Graph refuses $search on /users without ConsistencyLevel: eventual, with a bare 400.
        graph, rec, _ = make_client(json_response(200, {"value": [{"id": "1"}]}))
        async with graph:
            async for _ in graph.users.find("smith"):
                break

        self.assertEqual(rec.header("ConsistencyLevel"), "eventual")
        self.assertEqual(query_of(rec.last)["$search"], '"displayName:smith" OR "mail:smith"')

    async def test_the_consistency_header_survives_onto_the_next_page(self) -> None:
        # A header that qualifies the query has to hold for the whole walk, not just page one.
        page_two = "https://graph.microsoft.com/v1.0/users?$skiptoken=X"
        graph, rec, _ = make_client(
            json_response(200, {"value": [{"id": "1"}], "@odata.nextLink": page_two}),
            json_response(200, {"value": [{"id": "2"}]}),
        )
        async with graph:
            found = [person async for person in graph.users.find("smith")]

        self.assertEqual([p["id"] for p in found], ["1", "2"])
        self.assertEqual(len(rec.requests), 2)
        self.assertEqual(rec.header("ConsistencyLevel", 1), "eventual")

    async def test_an_empty_search_is_refused_before_any_request(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError) as raised:
                graph.users.find("")
        self.assertEqual(raised.exception.code, "invalidRequest")
        self.assertEqual(rec.requests, [])

    async def test_by_email_filters_on_mail_not_on_the_principal_name(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": [{"id": "1", "mail": "a@x.com"}]}))
        async with graph:
            person = await graph.users.by_email("a@x.com")

        self.assertEqual(path_of(rec.last), "/v1.0/users")
        self.assertEqual(query_of(rec.last)["$filter"], "mail eq 'a@x.com'")
        self.assertEqual(person["id"], "1")

    async def test_by_email_raises_rather_than_returning_nothing(self) -> None:
        graph, _, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.users.by_email("nobody@x.com")
        self.assertEqual(raised.exception.code, "itemNotFound")


class OrgChart(unittest.IsolatedAsyncioTestCase):
    async def test_me_is_the_default_and_anyone_else_is_addressable(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            await graph.users.manager()
            await graph.users.manager("bob@x.com")
            async for _ in graph.users.reports():
                break
            async for _ in graph.users.groups("bob@x.com"):
                break

        self.assertEqual([path_of(r) for r in rec.requests], [
            "/v1.0/me/manager",
            "/v1.0/users/bob@x.com/manager",
            "/v1.0/me/directReports",
            "/v1.0/users/bob@x.com/memberOf",
        ])

    async def test_a_photo_streams_to_disk_at_the_size_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "face.jpg"
            graph, rec, _ = make_client()
            rec.answer = lambda request: httpx.Response(200, content=b"\xff\xd8jpeg")

            async with graph:
                await graph.users.photo(str(destination))
                first = path_of(rec.last)
                await graph.users.photo(str(destination), user="bob@x.com", size="96x96")

            self.assertEqual(destination.read_bytes(), b"\xff\xd8jpeg")

        self.assertEqual(first, "/v1.0/me/photo/$value")
        self.assertEqual(path_of(rec.last), "/v1.0/users/bob@x.com/photos/96x96/$value")


# ── files ────────────────────────────────────────────────────────────────────


class DriveAddressing(unittest.IsolatedAsyncioTestCase):
    async def test_a_leading_slash_is_a_path_and_anything_else_is_an_id(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"id": "1"}))
        async with graph:
            await graph.files.metadata("/reports/q3.xlsx")
            by_path = path_of(rec.last)
            await graph.files.metadata("01ABCDEF")
            by_id = path_of(rec.last)
            async for _ in graph.files.folder():
                break

        # The closing colon is what separates the path from the segment after it.
        self.assertEqual(by_path, "/v1.0/me/drive/root:/reports/q3.xlsx:")
        self.assertEqual(by_id, "/v1.0/me/drive/items/01ABCDEF")
        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/root/children")

    async def test_an_empty_item_is_refused(self) -> None:
        graph, _, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.files.metadata("   ")
        self.assertEqual(raised.exception.code, "invalidRequest")


class DriveTransfers(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="msgraph-drive-"))

    def tearDown(self) -> None:
        for leftover in self.workspace.glob("*"):
            leftover.unlink(missing_ok=True)
        self.workspace.rmdir()

    async def test_upload_keeps_the_local_name_at_the_root_by_default(self) -> None:
        source = self.workspace / "q3.xlsx"
        source.write_bytes(b"spreadsheet")

        graph, rec, _ = make_client(json_response(201, {"id": "1", "name": "q3.xlsx"}))
        async with graph:
            await graph.files.upload(source)

        self.assertEqual(rec.last.method, "PUT")
        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/root:/q3.xlsx:/content")
        self.assertEqual(rec.last.content, b"spreadsheet")

    async def test_upload_honours_an_explicit_destination(self) -> None:
        source = self.workspace / "q3.xlsx"
        source.write_bytes(b"x")

        graph, rec, _ = make_client(json_response(201, {"id": "1"}))
        async with graph:
            await graph.files.upload(source, to="/reports/2026/q3.xlsx")

        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/root:/reports/2026/q3.xlsx:/content")

    async def test_a_large_upload_still_reaches_createuploadsession(self) -> None:
        # The path Files builds has to survive to_session_path, which rewrites the :/content
        # suffix. Anything appended after it -- a query string, say -- would break that silently.
        source = self.workspace / "big.bin"
        source.write_bytes(b"0" * (5 * 1024 * 1024))

        graph, rec, _ = make_client()
        rec.answer = lambda request: (
            json_response(200, {"uploadUrl": "https://upload.contoso.com/session"})
            if request.method == "POST"
            else httpx.Response(201, json={"id": "1"})
        )
        async with graph:
            await graph.files.upload(source, to="/big.bin")

        self.assertEqual(rec.requests[0].method, "POST")
        self.assertEqual(path_of(rec.requests[0]),
                         "/v1.0/me/drive/root:/big.bin:/createUploadSession")

    async def test_a_missing_source_is_refused_before_any_request(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.files.upload(self.workspace / "absent.txt")
        self.assertEqual(raised.exception.code, "invalidRequest")
        self.assertEqual(rec.requests, [])

    async def test_download_reads_the_content_endpoint(self) -> None:
        destination = self.workspace / "out.xlsx"
        graph, rec, _ = make_client()
        rec.answer = lambda request: httpx.Response(200, content=b"bytes")

        async with graph:
            await graph.files.download("/reports/q3.xlsx", destination)

        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/root:/reports/q3.xlsx:/content")
        self.assertEqual(destination.read_bytes(), b"bytes")


class DriveChanges(unittest.IsolatedAsyncioTestCase):
    async def test_make_folder_posts_to_the_parent(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "1"}))
        async with graph:
            await graph.files.make_folder("/reports/2026")

        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/root:/reports:/children")
        self.assertEqual(json.loads(rec.last.content), {
            "name": "2026",
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        })

    async def test_a_top_level_folder_posts_to_the_root(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "1"}))
        async with graph:
            await graph.files.make_folder("/reports", conflict="rename")

        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/root/children")
        self.assertEqual(json.loads(rec.last.content)["@microsoft.graph.conflictBehavior"],
                         "rename")

    async def test_an_unknown_conflict_behaviour_is_refused(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError):
                await graph.files.make_folder("/x", conflict="overwrite")
        self.assertEqual(rec.requests, [])

    async def test_share_link_returns_just_the_url(self) -> None:
        graph, rec, _ = make_client(
            json_response(201, {"link": {"webUrl": "https://contoso.sharepoint.com/s/abc"}})
        )
        async with graph:
            url = await graph.files.share_link("/q3.xlsx", kind="edit")

        self.assertEqual(url, "https://contoso.sharepoint.com/s/abc")
        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/root:/q3.xlsx:/createLink")
        self.assertEqual(json.loads(rec.last.content), {"type": "edit", "scope": "organization"})

    async def test_an_unknown_link_kind_or_scope_is_refused(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            for bad in ({"kind": "download"}, {"scope": "everyone"}):
                with self.subTest(**bad):
                    with self.assertRaises(GraphError):
                        await graph.files.share_link("/q3.xlsx", **bad)
        self.assertEqual(rec.requests, [])

    async def test_search_escapes_a_quote_in_the_term(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            async for _ in graph.files.search("o'brien"):
                break

        self.assertIn("/search(q='o''brien')", path_of(rec.last))

    async def test_remove_deletes_the_item(self) -> None:
        graph, rec, _ = make_client(json_response(204))
        async with graph:
            await graph.files.remove("01ABCDEF")

        self.assertEqual(rec.last.method, "DELETE")
        self.assertEqual(path_of(rec.last), "/v1.0/me/drive/items/01ABCDEF")


# ── teams ────────────────────────────────────────────────────────────────────


class ChannelMessages(unittest.IsolatedAsyncioTestCase):
    async def test_post_builds_the_chatmessage_body(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "m1"}))
        async with graph:
            await graph.teams.post("t1", "c1", "Deploy is green")

        self.assertEqual(path_of(rec.last), "/v1.0/teams/t1/channels/c1/messages")
        self.assertEqual(json.loads(rec.last.content), {
            "body": {"contentType": "text", "content": "Deploy is green"},
        })

    async def test_html_subject_and_importance_are_carried(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "m1"}))
        async with graph:
            await graph.teams.post("t1", "c1", "<b>down</b>", html=True,
                                   subject="Incident", importance="urgent")

        self.assertEqual(json.loads(rec.last.content), {
            "body": {"contentType": "html", "content": "<b>down</b>"},
            "subject": "Incident",
            "importance": "urgent",
        })

    async def test_normal_importance_is_left_out_rather_than_sent(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "m1"}))
        async with graph:
            await graph.teams.post("t1", "c1", "hi")
        self.assertNotIn("importance", json.loads(rec.last.content))

    async def test_an_empty_message_or_bad_importance_is_refused(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError):
                await graph.teams.post("t1", "c1", "")
            with self.assertRaises(GraphError):
                await graph.teams.post("t1", "c1", "hi", importance="shouting")
        self.assertEqual(rec.requests, [])

    async def test_reply_attaches_to_the_thread_root(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "m2"}))
        async with graph:
            await graph.teams.reply("t1", "c1", "m1", "on it")

        self.assertEqual(path_of(rec.last), "/v1.0/teams/t1/channels/c1/messages/m1/replies")

    async def test_send_chat_uses_the_same_message_shape(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "m1"}))
        async with graph:
            await graph.teams.send_chat("chat1", "hello")

        self.assertEqual(path_of(rec.last), "/v1.0/chats/chat1/messages")
        self.assertEqual(json.loads(rec.last.content),
                         {"body": {"contentType": "text", "content": "hello"}})


class TeamNavigation(unittest.IsolatedAsyncioTestCase):
    async def test_mine_and_chats_read_the_signed_in_person(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            async for _ in graph.teams.mine():
                break
            first = path_of(rec.last)
            async for _ in graph.teams.chats():
                break

        self.assertEqual(first, "/v1.0/me/joinedTeams")
        self.assertEqual(path_of(rec.last), "/v1.0/me/chats")

    async def test_channel_by_name_ignores_case(self) -> None:
        graph, _, _ = make_client(json_response(200, {"value": [
            {"id": "c1", "displayName": "General"},
            {"id": "c2", "displayName": "Deploys"},
        ]}))
        async with graph:
            channel = await graph.teams.channel_by_name("t1", "deploys")
        self.assertEqual(channel["id"], "c2")

    async def test_a_missing_channel_says_so(self) -> None:
        graph, _, _ = make_client(json_response(200, {"value": [{"id": "c1",
                                                                 "displayName": "General"}]}))
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.teams.channel_by_name("t1", "nowhere")
        self.assertEqual(raised.exception.code, "itemNotFound")


if __name__ == "__main__":
    unittest.main(verbosity=2)
