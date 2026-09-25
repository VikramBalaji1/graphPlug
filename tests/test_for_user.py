"""Acting on a named person's mailbox, calendar and drive.

Under application access there is no /me, so every resource method takes ``user`` and re-points
its /me path at /users/{user}. These tests pin the exact paths, including that leaving ``user`` out
still means /me, and that an address with a '#' in it (a guest's #EXT#) survives encoding.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import httpx

from _support import json_response, make_client

from graphplug._resources import GraphResource

SENDER = "reports@contoso.com"
AS_SENDER = "/v1.0/users/reports@contoso.com"


def path_of(request: httpx.Request) -> str:
    return unquote(urlsplit(str(request.url)).path)


class ForUser(unittest.TestCase):
    def test_a_me_path_is_re_pointed_at_the_user(self) -> None:
        self.assertEqual(GraphResource._for("/me/events", SENDER), f"/users/{SENDER}/events")
        self.assertEqual(GraphResource._for("/me", SENDER), f"/users/{SENDER}")

    def test_no_user_or_me_keeps_me(self) -> None:
        self.assertEqual(GraphResource._for("/me/events", None), "/me/events")
        self.assertEqual(GraphResource._for("/me/events", "me"), "/me/events")

    def test_paths_not_under_me_are_left_alone(self) -> None:
        self.assertEqual(GraphResource._for("/teams", SENDER), "/teams")
        self.assertEqual(GraphResource._for("/messages", SENDER), "/messages")

    def test_a_guest_address_is_encoded(self) -> None:
        self.assertEqual(GraphResource._for("/me/drive", "bob_x.com#EXT#@t.onmicrosoft.com"),
                         "/users/bob_x.com%23EXT%23@t.onmicrosoft.com/drive")


class MailForUser(unittest.IsolatedAsyncioTestCase):
    async def test_send_goes_out_from_the_named_mailbox(self) -> None:
        graph, rec, _ = make_client(json_response(202))
        async with graph:
            await graph.mail.send(to="a@contoso.com", subject="Hi", body="x", user=SENDER)
        self.assertEqual(path_of(rec.last), f"{AS_SENDER}/sendMail")
        self.assertEqual(json.loads(rec.last.content)["message"]["subject"], "Hi")

    async def test_send_without_user_still_uses_me(self) -> None:
        graph, rec, _ = make_client(json_response(202))
        async with graph:
            await graph.mail.send(to="a@contoso.com", subject="Hi")
        self.assertEqual(path_of(rec.last), "/v1.0/me/sendMail")

    async def test_send_many_takes_a_default_and_a_per_message_sender(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"responses": []}))
        async with graph:
            await graph.mail.send_many([
                {"to": "a@contoso.com", "subject": "A"},
                {"to": "b@contoso.com", "subject": "B", "user": "hr@contoso.com"},
            ], user=SENDER)
        urls = [r["url"] for r in json.loads(rec.last.content)["requests"]]
        self.assertEqual(urls, [f"/users/{SENDER}/sendMail", "/users/hr@contoso.com/sendMail"])

    async def test_reading_and_tidying_follow_the_user(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            [m async for m in graph.mail.inbox(unread_only=True, user=SENDER)]
            await graph.mail.reply("m1", "ok", user=SENDER)
            await graph.mail.forward("m1", to="c@contoso.com", user=SENDER)
            await graph.mail.mark_read("m1", user=SENDER)
            await graph.mail.move("m1", "archive", user=SENDER)
        self.assertEqual([path_of(r) for r in rec.requests], [
            f"{AS_SENDER}/mailFolders/inbox/messages",
            f"{AS_SENDER}/messages/m1/reply",
            f"{AS_SENDER}/messages/m1/forward",
            f"{AS_SENDER}/messages/m1",
            f"{AS_SENDER}/messages/m1/move",
        ])


class CalendarForUser(unittest.IsolatedAsyncioTestCase):
    START = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    async def test_schedule_blocks_the_named_calendar(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "e1"}))
        async with graph:
            await graph.calendar.schedule(
                subject="Focus time", start=self.START, end=self.START + timedelta(hours=2),
                user=SENDER,
            )
        self.assertEqual(path_of(rec.last), f"{AS_SENDER}/events")
        body = json.loads(rec.last.content)
        self.assertEqual(body["subject"], "Focus time")
        self.assertNotIn("user", body, "user chooses the calendar; it is not an event field")

    async def test_schedule_many_takes_a_default_and_a_per_event_calendar(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"responses": []}))
        end = self.START + timedelta(hours=1)
        async with graph:
            await graph.calendar.schedule_many([
                {"subject": "A", "start": self.START, "end": end},
                {"subject": "B", "start": self.START, "end": end, "user": "room.b@contoso.com"},
            ], user=SENDER)
        urls = [r["url"] for r in json.loads(rec.last.content)["requests"]]
        self.assertEqual(urls, [f"/users/{SENDER}/events", "/users/room.b@contoso.com/events"])

    async def test_reading_responding_and_availability_follow_the_user(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            [e async for e in graph.calendar.upcoming(user=SENDER)]
            await graph.calendar.respond("e1", "accept", user=SENDER)
            await graph.calendar.cancel("e1", user=SENDER)
            await graph.calendar.find_times(["a@contoso.com"], user=SENDER)
            await graph.calendar.free_busy(["a@contoso.com"], self.START,
                                           self.START + timedelta(hours=8), user=SENDER)
        self.assertEqual([path_of(r) for r in rec.requests], [
            f"{AS_SENDER}/calendarView",
            f"{AS_SENDER}/events/e1/accept",
            f"{AS_SENDER}/events/e1/cancel",
            f"{AS_SENDER}/findMeetingTimes",
            f"{AS_SENDER}/calendar/getSchedule",
        ])


class FilesForUser(unittest.IsolatedAsyncioTestCase):
    async def test_every_drive_call_uses_the_named_drive(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": [], "link": {"webUrl": "u"}}))
        with tempfile.TemporaryDirectory() as folder:
            local = os.path.join(folder, "q3.txt")
            with open(local, "wb") as handle:
                handle.write(b"numbers")
            async with graph:
                await graph.files.upload(local, to="/reports/q3.txt", user=SENDER)
                await graph.files.metadata("/reports/q3.txt", user=SENDER)
                [i async for i in graph.files.folder("/reports", user=SENDER)]
                [i async for i in graph.files.search("q3", user=SENDER)]
                await graph.files.make_folder("/reports/2026", user=SENDER)
                await graph.files.share_link("/reports/q3.txt", user=SENDER)
                await graph.files.remove("ITEM1", user=SENDER)
        self.assertEqual([path_of(r) for r in rec.requests], [
            f"{AS_SENDER}/drive/root:/reports/q3.txt:/content",
            f"{AS_SENDER}/drive/root:/reports/q3.txt:",
            f"{AS_SENDER}/drive/root:/reports:/children",
            f"{AS_SENDER}/drive/root/search(q='q3')",
            f"{AS_SENDER}/drive/root:/reports:/children",
            f"{AS_SENDER}/drive/root:/reports/q3.txt:/createLink",
            f"{AS_SENDER}/drive/items/ITEM1",
        ])


class TeamsAndSharedMethodsForUser(unittest.IsolatedAsyncioTestCase):
    async def test_teams_and_chats_list_for_the_user(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            [t async for t in graph.teams.mine(user=SENDER)]
            [c async for c in graph.teams.chats(user=SENDER)]
        self.assertEqual([path_of(r) for r in rec.requests],
                         [f"{AS_SENDER}/joinedTeams", f"{AS_SENDER}/chats"])

    async def test_the_shared_methods_take_user_too(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            [m async for m in graph.mail.list(user=SENDER, top=5)]
            await graph.mail.get("m1", user=SENDER)
            await graph.calendar.update("e1", {"subject": "x"}, user=SENDER)
            await graph.calendar.delete("e1", user=SENDER)
            await graph.mail.create({"subject": "draft"}, user=SENDER)
        self.assertEqual([path_of(r) for r in rec.requests], [
            f"{AS_SENDER}/messages",
            f"{AS_SENDER}/messages/m1",
            f"{AS_SENDER}/events/e1",
            f"{AS_SENDER}/events/e1",
            f"{AS_SENDER}/messages",
        ])
        self.assertEqual(rec.requests[0].url.params.get("$top"), "5")

    async def test_get_many_batches_under_the_user(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"responses": []}))
        async with graph:
            await graph.mail.get_many(["m1", "m2"], select="subject", user=SENDER)
        urls = [r["url"] for r in json.loads(rec.last.content)["requests"]]
        self.assertEqual(urls, [f"/users/{SENDER}/messages/m1?$select=subject",
                                f"/users/{SENDER}/messages/m2?$select=subject"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
