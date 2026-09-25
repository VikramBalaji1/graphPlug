"""Mail and Calendar: the payloads they build.

These are the tests that matter most for the resource layer. Its whole value is producing the
nested JSON Graph wants, so the assertions are field by field against what goes on the wire.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from _support import json_response, make_client

from graphplug import GraphError, Scopes


class MailPayloads(unittest.IsolatedAsyncioTestCase):
    async def test_send_builds_the_sendmail_payload(self) -> None:
        graph, rec, _ = make_client(json_response(202))
        async with graph:
            await graph.mail.send(to="alice@contoso.com", subject="Hi", body="Hello")

        self.assertTrue(str(rec.last.url).endswith("/me/sendMail"))
        payload = json.loads(rec.last.content)
        self.assertEqual(payload["saveToSentItems"], True)
        self.assertEqual(payload["message"], {
            "subject": "Hi",
            "body": {"contentType": "Text", "content": "Hello"},
            "toRecipients": [{"emailAddress": {"address": "alice@contoso.com"}}],
        })

    async def test_one_recipient_or_many(self) -> None:
        graph, rec, _ = make_client(json_response(202))
        async with graph:
            await graph.mail.send(to=["a@x.com", "b@x.com"], cc="c@x.com", bcc=["d@x.com"],
                                  subject="s")

        message = json.loads(rec.last.content)["message"]
        self.assertEqual([r["emailAddress"]["address"] for r in message["toRecipients"]],
                         ["a@x.com", "b@x.com"])
        self.assertEqual(message["ccRecipients"][0]["emailAddress"]["address"], "c@x.com")
        self.assertEqual(message["bccRecipients"][0]["emailAddress"]["address"], "d@x.com")

    async def test_html_switches_the_content_type(self) -> None:
        graph, rec, _ = make_client(json_response(202))
        async with graph:
            await graph.mail.send(to="a@x.com", subject="s", body="<b>hi</b>", html=True)

        self.assertEqual(json.loads(rec.last.content)["message"]["body"]["contentType"], "HTML")

    async def test_attachments_are_base64_with_the_odata_type(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.pdf"
            report.write_bytes(b"%PDF-1.4 fake")

            graph, rec, _ = make_client(json_response(202))
            async with graph:
                await graph.mail.send(to="a@x.com", subject="s", attachments=[report])

            attachment = json.loads(rec.last.content)["message"]["attachments"][0]

        self.assertEqual(attachment["@odata.type"], "#microsoft.graph.fileAttachment")
        self.assertEqual(attachment["name"], "report.pdf")
        self.assertEqual(attachment["contentType"], "application/pdf")
        self.assertEqual(base64.b64decode(attachment["contentBytes"]), b"%PDF-1.4 fake")

    async def test_an_oversized_attachment_is_refused_with_advice(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            big = Path(folder) / "big.bin"
            big.write_bytes(b"0" * (4 * 1024 * 1024))

            graph, rec, _ = make_client(json_response(202))
            async with graph:
                with self.assertRaises(GraphError) as raised:
                    await graph.mail.send(to="a@x.com", subject="s", attachments=[big])

        self.assertIn("OneDrive", raised.exception.message)
        self.assertEqual(rec.requests, [], "nothing should reach the wire")

    async def test_a_missing_recipient_is_refused(self) -> None:
        graph, _, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError):
                await graph.mail.send(to=None, subject="s")

    async def test_send_many_batches(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            sent = json.loads(request.content)["requests"]
            return json_response(200, {"responses": [
                {"id": item["id"], "status": 202} for item in sent
            ]})

        graph, rec, _ = make_client()
        rec.answer = answer

        async with graph:
            results = await graph.mail.send_many([
                {"to": f"user{i}@x.com", "subject": f"n{i}"} for i in range(25)
            ])

        self.assertEqual(len(results), 25)
        sizes = [len(json.loads(r.content)["requests"]) for r in rec.requests]
        self.assertEqual(sorted(sizes, reverse=True), [20, 5])

    async def test_reply_and_forward_hit_the_right_actions(self) -> None:
        graph, rec, _ = make_client(json_response(202))
        async with graph:
            await graph.mail.reply("m1", comment="Thanks")
            await graph.mail.reply("m1", comment="All", reply_all=True)
            await graph.mail.forward("m1", to="b@x.com", comment="FYI")

        self.assertTrue(str(rec.requests[0].url).endswith("/me/messages/m1/reply"))
        self.assertTrue(str(rec.requests[1].url).endswith("/me/messages/m1/replyAll"))
        self.assertTrue(str(rec.requests[2].url).endswith("/me/messages/m1/forward"))
        self.assertEqual(json.loads(rec.requests[2].content)["toRecipients"][0]["emailAddress"]
                         ["address"], "b@x.com")

    async def test_the_inbox_filters_and_orders(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            [m async for m in graph.mail.inbox(unread_only=True)]

        query = parse_qs(urlsplit(str(rec.last.url)).query)
        self.assertIn("/me/mailFolders/inbox/messages", str(rec.last.url))
        self.assertEqual(query["$filter"], ["isRead eq false"])
        self.assertEqual(query["$orderby"], ["receivedDateTime desc"])

    async def test_search_replaces_filter_because_graph_forbids_both(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            [m async for m in graph.mail.inbox(unread_only=True, search="invoice")]

        query = parse_qs(urlsplit(str(rec.last.url)).query)
        self.assertEqual(query["$search"], ['"invoice"'])
        self.assertNotIn("$filter", query)
        self.assertNotIn("$orderby", query)


class CalendarPayloads(unittest.IsolatedAsyncioTestCase):
    START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    END = datetime(2026, 3, 2, 9, 30, tzinfo=timezone.utc)

    async def test_schedule_builds_the_event(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "e1"}))
        async with graph:
            await graph.calendar.schedule(
                subject="Standup", start=self.START, end=self.END, attendees=["a@x.com"]
            )

        payload = json.loads(rec.last.content)
        self.assertEqual(payload["subject"], "Standup")
        self.assertEqual(payload["start"], {"dateTime": "2026-03-02T09:00:00", "timeZone": "UTC"})
        self.assertEqual(payload["end"], {"dateTime": "2026-03-02T09:30:00", "timeZone": "UTC"})
        self.assertEqual(payload["attendees"], [
            {"emailAddress": {"address": "a@x.com"}, "type": "required"}
        ])

    async def test_online_sets_both_fields_teams_needs(self) -> None:
        # isOnlineMeeting alone produces an event with no join link.
        graph, rec, _ = make_client(json_response(201, {"id": "e1"}))
        async with graph:
            await graph.calendar.schedule(
                subject="Sync", start=self.START, end=self.END, online=True
            )

        payload = json.loads(rec.last.content)
        self.assertIs(payload["isOnlineMeeting"], True)
        self.assertEqual(payload["onlineMeetingProvider"], "teamsForBusiness")

    async def test_optional_attendees_carry_their_type(self) -> None:
        graph, rec, _ = make_client(json_response(201, {}))
        async with graph:
            await graph.calendar.schedule(
                subject="s", start=self.START, end=self.END,
                attendees="a@x.com", optional_attendees=["b@x.com"],
            )

        kinds = {a["emailAddress"]["address"]: a["type"]
                 for a in json.loads(rec.last.content)["attendees"]}
        self.assertEqual(kinds, {"a@x.com": "required", "b@x.com": "optional"})

    async def test_an_end_before_the_start_is_refused(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError):
                await graph.calendar.schedule(subject="s", start=self.END, end=self.START)
        self.assertEqual(rec.requests, [])

    async def test_a_naive_time_keeps_the_named_zone(self) -> None:
        graph, rec, _ = make_client(json_response(201, {}))
        async with graph:
            await graph.calendar.schedule(
                subject="s",
                start=datetime(2026, 3, 2, 9, 0),
                end=datetime(2026, 3, 2, 10, 0),
                timezone_name="India Standard Time",
            )

        self.assertEqual(json.loads(rec.last.content)["start"]["timeZone"], "India Standard Time")

    async def test_upcoming_uses_calendar_view(self) -> None:
        # /events returns the series master; calendarView expands occurrences.
        graph, rec, _ = make_client(json_response(200, {"value": []}))
        async with graph:
            [e async for e in graph.calendar.upcoming(days=3)]

        url = str(rec.last.url)
        query = parse_qs(urlsplit(url).query)
        self.assertIn("/me/calendarView", url)
        self.assertIn("startDateTime", query)
        self.assertIn("endDateTime", query)
        self.assertEqual(query["$orderby"], ["start/dateTime"])

    async def test_respond_rejects_an_unknown_answer(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError):
                await graph.calendar.respond("e1", "maybe")
        self.assertEqual(rec.requests, [])

    async def test_respond_and_cancel_hit_the_right_actions(self) -> None:
        graph, rec, _ = make_client(json_response(202))
        async with graph:
            await graph.calendar.respond("e1", "accept", comment="see you")
            await graph.calendar.cancel("e1", comment="clash")

        self.assertTrue(str(rec.requests[0].url).endswith("/me/events/e1/accept"))
        self.assertTrue(str(rec.requests[1].url).endswith("/me/events/e1/cancel"))

    async def test_find_times_asks_for_suggestions(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"meetingTimeSuggestions": [{"x": 1}]}))
        async with graph:
            suggestions = await graph.calendar.find_times(["a@x.com"], duration_minutes=45)

        payload = json.loads(rec.last.content)
        self.assertTrue(str(rec.last.url).endswith("/me/findMeetingTimes"))
        self.assertEqual(payload["meetingDuration"], "PT45M")
        self.assertEqual(suggestions, [{"x": 1}])


class SharedBehaviour(unittest.IsolatedAsyncioTestCase):
    async def test_every_resource_gets_the_same_five_operations(self) -> None:
        graph, rec, _ = make_client(json_response(200, {"id": "1"}))
        async with graph:
            await graph.mail.get("m1")
            await graph.calendar.get("e1")

        self.assertTrue(str(rec.requests[0].url).endswith("/me/messages/m1"))
        self.assertTrue(str(rec.requests[1].url).endswith("/me/events/e1"))

    async def test_get_many_batches_and_keeps_order(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            sent = json.loads(request.content)["requests"]
            return json_response(200, {"responses": [
                {"id": item["id"], "status": 200, "body": {"url": item["url"]}}
                for item in reversed(sent)
            ]})

        graph, rec, _ = make_client()
        rec.answer = answer

        async with graph:
            results = await graph.mail.get_many([f"m{i}" for i in range(23)])

        self.assertEqual(len(results), 23)
        self.assertEqual(results[0]["body"]["url"], "/me/messages/m0")
        self.assertEqual(results[22]["body"]["url"], "/me/messages/m22")

    async def test_resources_declare_their_scopes(self) -> None:
        graph, _, _ = make_client()
        async with graph:
            self.assertIn("Mail.Send", graph.mail.scopes)
            self.assertIn("Calendars.ReadWrite", graph.calendar.scopes)

    def test_scopes_combine_without_duplicates(self) -> None:
        combined = Scopes.combine(Scopes.MAIL_SEND, Scopes.MAIL_READ_WRITE, Scopes.MAIL_SEND)
        self.assertEqual(combined, ("Mail.Send", "Mail.ReadWrite"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
