"""Calendar and meetings.

As with mail, the value here is the payload. A Teams meeting needs ``isOnlineMeeting`` paired with
``onlineMeetingProvider``; attendees are objects carrying a ``type``; and every time is a
``dateTime``/``timeZone`` pair rather than an ISO string. None of that is guessable from the call
site, so it lives here once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence, Union

from .._errors import GraphError
from .._request import build_url, odata
from .._scopes import Scopes
from .base import GraphResource

__all__ = ["Calendar"]

Attendees = Union[str, Sequence[str], None]

_RESPONSES = {"accept", "decline", "tentativelyAccept"}


def _graph_time(moment: datetime, tz: str) -> Dict[str, str]:
    """Graph wants a naive local time plus a named zone, not an offset."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        tz = "UTC"
    return {"dateTime": moment.isoformat(timespec="seconds"), "timeZone": tz}


def _attendees(value: Attendees, kind: str = "required") -> List[Dict[str, Any]]:
    if not value:
        return []
    addresses = [value] if isinstance(value, str) else list(value)
    return [
        {"emailAddress": {"address": address}, "type": kind}
        for address in addresses
    ]


class Calendar(GraphResource):
    """Events on a calendar: the signed-in person's, or ``user``'s.

    Every method takes ``user`` (an id or mail address). Under application access there is no
    signed-in person, so pass it: ``schedule(..., user="room.a@contoso.com")`` books that calendar.
    """

    path = "/me/events"
    scopes = Scopes.CALENDARS_READ_WRITE

    # ── scheduling ───────────────────────────────────────────────────────────

    def compose(
        self,
        subject: str,
        start: datetime,
        end: datetime,
        attendees: Attendees = None,
        optional_attendees: Attendees = None,
        online: bool = False,
        location: Optional[str] = None,
        body: str = "",
        html: bool = False,
        timezone_name: str = "UTC",
        reminder_minutes: Optional[int] = None,
        all_day: bool = False,
    ) -> Dict[str, Any]:
        """Build the event payload without creating it."""
        if end <= start:
            raise GraphError(0, "invalidRequest", "'end' must be after 'start'")

        event: Dict[str, Any] = {
            "subject": subject,
            "start": _graph_time(start, timezone_name),
            "end": _graph_time(end, timezone_name),
        }

        people = _attendees(attendees) + _attendees(optional_attendees, "optional")
        if people:
            event["attendees"] = people
        if body:
            event["body"] = {"contentType": "HTML" if html else "Text", "content": body}
        if location:
            event["location"] = {"displayName": location}
        if reminder_minutes is not None:
            event["reminderMinutesBeforeStart"] = reminder_minutes
            event["isReminderOn"] = True
        if all_day:
            event["isAllDay"] = True

        if online:
            # Both fields are needed; isOnlineMeeting alone produces an event with no join link.
            event["isOnlineMeeting"] = True
            event["onlineMeetingProvider"] = "teamsForBusiness"

        return event

    async def schedule(self, user: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """Create an event, on ``user``'s calendar when given. Takes everything ``compose`` takes.

        The event shows as busy, which is what blocks the time. With ``online=True`` the response
        carries ``onlineMeeting.joinUrl``.
        """
        return await self.create(self.compose(**fields), user=user)

    async def schedule_many(
        self, events: Sequence[Dict[str, Any]], user: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Create many events in as few round-trips as Graph allows.

        Each entry is the keyword arguments ``compose`` takes, plus an optional ``user`` for that
        one event's calendar; ``user`` here is the default for the rest.
        """
        requests = []
        for fields in events:
            fields = dict(fields)
            owner = fields.pop("user", user)
            requests.append({
                "method": "POST",
                "url": self._for(self.path, owner),
                "headers": {"Content-Type": "application/json"},
                "body": self.compose(**fields),
            })
        return await self._client.batch(requests)

    # ── reading ──────────────────────────────────────────────────────────────

    def upcoming(
        self,
        days: int = 7,
        select: str = "id,subject,start,end,location,onlineMeeting,organizer,attendees",
        top: int = 50,
        user: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Events in the next ``days``, soonest first.

        Uses calendarView, which is the endpoint that expands recurring series -- listing /events
        returns the series master instead of its occurrences.
        """
        now = datetime.now(timezone.utc)
        query = odata({
            "select": select,
            "top": top,
            "orderby": "start/dateTime",
        })
        query["startDateTime"] = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        query["endDateTime"] = (now + timedelta(days=days)).isoformat(
            timespec="seconds").replace("+00:00", "Z")

        return self._client.paged(build_url(self._for("/me/calendarView", user), None, query))

    # ── responding ───────────────────────────────────────────────────────────

    async def respond(
        self, event_id: str, response: str, comment: str = "", send_response: bool = True,
        user: Optional[str] = None,
    ) -> None:
        """Accept, decline or tentatively accept an invitation."""
        if response not in _RESPONSES:
            raise GraphError(
                0, "invalidRequest", f"'response' must be one of {', '.join(sorted(_RESPONSES))}"
            )
        await self._action(event_id, response, {
            "comment": comment,
            "sendResponse": send_response,
        }, user=user)

    async def cancel(self, event_id: str, comment: str = "", user: Optional[str] = None) -> None:
        """Cancel a meeting you organise, notifying the attendees."""
        await self._action(event_id, "cancel", {"comment": comment}, user=user)

    # ── finding a slot ───────────────────────────────────────────────────────

    async def find_times(
        self,
        attendees: Attendees,
        duration_minutes: int = 30,
        within_days: int = 5,
        minimum_attendance_percent: int = 100,
        user: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Ask Graph for slots that suit everyone. Returns the suggestions, best first."""
        now = datetime.now(timezone.utc)
        suggestions = await self._collection_action("findMeetingTimes", {
            "attendees": _attendees(attendees),
            "timeConstraint": {
                "activityDomain": "work",
                "timeSlots": [{
                    "start": _graph_time(now, "UTC"),
                    "end": _graph_time(now + timedelta(days=within_days), "UTC"),
                }],
            },
            "meetingDuration": f"PT{duration_minutes}M",
            "minimumAttendeePercentage": minimum_attendance_percent,
            "returnSuggestionReasons": True,
        }, user=user)
        return (suggestions or {}).get("meetingTimeSuggestions", [])

    async def free_busy(
        self, people: Iterable[str], start: datetime, end: datetime, interval_minutes: int = 30,
        user: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Each person's availability over a window."""
        schedules = await self._collection_action("calendar/getSchedule", {
            "schedules": list(people),
            "startTime": _graph_time(start, "UTC"),
            "endTime": _graph_time(end, "UTC"),
            "availabilityViewInterval": interval_minutes,
        }, user=user)
        return (schedules or {}).get("value", [])
