"""schedule(..., auto_record=True): the event, then the Teams meeting behind it.

Recording is a property of the Teams onlineMeeting, not the calendar event, so schedule creates the
event, finds the meeting by its join link, and patches recordAutomatically. These tests pin that
sequence, the re-read when Teams is slow to fill the join link, and that a failure after the event
exists says so rather than hiding the meeting that was already sent out.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import httpx

from _support import json_response, make_client

from graphplug import GraphError

ORGANISER = "you@example.com"
AS_ORGANISER = f"/v1.0/users/{ORGANISER}"
JOIN = "https://teams.microsoft.com/l/meetup-join/abc"
START = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def path_of(request: httpx.Request) -> str:
    return unquote(urlsplit(str(request.url)).path)


def meeting(**extra):
    return {
        "subject": "Review", "start": START, "end": START + timedelta(hours=1),
        "attendees": ["alice@example.com"], "optional_attendees": ["carol@example.com"],
        "online": True, "user": ORGANISER, **extra,
    }


class AutoRecord(unittest.IsolatedAsyncioTestCase):
    async def test_the_event_is_created_then_the_teams_meeting_is_set_to_record(self) -> None:
        graph, rec, _ = make_client(
            json_response(201, {"id": "e1", "onlineMeeting": {"joinUrl": JOIN}}),
            json_response(200, {"value": [{"id": "m1"}]}),
            json_response(200, {"id": "m1", "recordAutomatically": True}),
        )
        async with graph:
            event = await graph.calendar.schedule(auto_record=True, **meeting())

        self.assertEqual(event["id"], "e1", "the caller still gets the event back")
        self.assertEqual([(r.method, path_of(r)) for r in rec.requests], [
            ("POST", f"{AS_ORGANISER}/events"),
            ("GET", f"{AS_ORGANISER}/onlineMeetings"),
            ("PATCH", f"{AS_ORGANISER}/onlineMeetings/m1"),
        ])
        self.assertEqual(rec.requests[1].url.params["$filter"], f"JoinWebUrl eq '{JOIN}'")
        self.assertEqual(json.loads(rec.requests[2].content), {"recordAutomatically": True})

    async def test_attendees_still_go_on_the_event_so_exchange_sends_invitations(self) -> None:
        graph, rec, _ = make_client(json_response(201, {"id": "e1"}))
        async with graph:
            await graph.calendar.schedule(**meeting())
        sent = json.loads(rec.requests[0].content)
        self.assertEqual([(a["emailAddress"]["address"], a["type"]) for a in sent["attendees"]],
                         [("alice@example.com", "required"), ("carol@example.com", "optional")])
        self.assertNotIn("auto_record", sent)
        self.assertEqual(len(rec.requests), 1, "no recording calls unless asked")

    async def test_a_late_join_link_is_read_again_from_the_event(self) -> None:
        graph, rec, _ = make_client(
            json_response(201, {"id": "e1", "onlineMeeting": None}),
            json_response(200, {"id": "e1", "onlineMeeting": {"joinUrl": JOIN}}),
            json_response(200, {"value": [{"id": "m1"}]}),
            json_response(200, {}),
        )
        async with graph:
            event = await graph.calendar.schedule(auto_record=True, **meeting())
        self.assertEqual(path_of(rec.requests[1]), f"{AS_ORGANISER}/events/e1")
        self.assertEqual(event["onlineMeeting"]["joinUrl"], JOIN)

    async def test_auto_record_without_a_teams_meeting_is_refused_before_anything_is_sent(self) -> None:
        graph, rec, _ = make_client()
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.calendar.schedule(auto_record=True, **meeting(online=False))
        self.assertEqual(raised.exception.code, "invalidRequest")
        self.assertEqual(rec.requests, [])

    async def test_a_failure_after_creation_names_the_event_that_already_exists(self) -> None:
        graph, _, _ = make_client(
            json_response(201, {"id": "e1", "onlineMeeting": {"joinUrl": JOIN}}),
            json_response(403, {"error": {"code": "Forbidden", "message": "No policy"}}),
        )
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.calendar.schedule(auto_record=True, **meeting())
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("event id e1", raised.exception.message)
        self.assertIn("No policy", raised.exception.message)

    async def test_no_matching_teams_meeting_is_reported(self) -> None:
        graph, _, _ = make_client(
            json_response(201, {"id": "e1", "onlineMeeting": {"joinUrl": JOIN}}),
            json_response(200, {"value": []}),
        )
        async with graph:
            with self.assertRaises(GraphError) as raised:
                await graph.calendar.schedule(auto_record=True, **meeting())
        self.assertEqual(raised.exception.code, "itemNotFound")


if __name__ == "__main__":
    unittest.main(verbosity=2)
