"""Find a slot that suits everyone, then book a Teams meeting in it.

    Needs:  Calendars.ReadWrite as a DELEGATED permission
    Run:    export AZURE_TENANT_ID=... AZURE_CLIENT_ID=...
            python samples/schedule_meeting.py alice@contoso.com bob@contoso.com
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from graphplug import GraphClient, GraphError, Scopes


async def main(attendees: list) -> int:
    tenant, client = os.environ.get("AZURE_TENANT_ID"), os.environ.get("AZURE_CLIENT_ID")
    if not tenant or not client:
        print("set AZURE_TENANT_ID and AZURE_CLIENT_ID", file=sys.stderr)
        return 2

    graph = await GraphClient.device_code(tenant, client, Scopes.CALENDARS_READ_WRITE)

    async with graph:
        try:
            # Ask Graph when everyone is free rather than guessing.
            suggestions = await graph.calendar.find_times(attendees, duration_minutes=30)
            if suggestions:
                slot = suggestions[0]["meetingTimeSlot"]
                start = datetime.fromisoformat(slot["start"]["dateTime"][:19]).replace(
                    tzinfo=timezone.utc)
                print(f"best slot: {start:%a %d %b %H:%M} UTC "
                      f"(confidence {suggestions[0].get('confidence', 0):.0f}%)")
            else:
                start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
                print("no common slot found; booking tomorrow instead")

            event = await graph.calendar.schedule(
                subject="Project sync",
                start=start,
                end=start + timedelta(minutes=30),
                attendees=attendees,
                online=True,                 # adds the Teams join link
                body="Agenda: progress, blockers, next steps.",
            )

            print(f"booked: {event['subject']}")
            join = (event.get("onlineMeeting") or {}).get("joinUrl")
            print(f"join  : {join}" if join else "join  : (no link returned)")

            print("\nnext 7 days:")
            async for upcoming in graph.calendar.upcoming(days=7):
                when = upcoming["start"]["dateTime"][:16].replace("T", " ")
                print(f"  {when}  {upcoming['subject']}")

        except GraphError as error:
            print(f"failed: [{error.status} {error.code}] {error.message}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python samples/schedule_meeting.py <attendee> [attendee ...]",
              file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
