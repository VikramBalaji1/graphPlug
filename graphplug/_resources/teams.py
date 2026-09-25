"""Teams: channels, channel messages and chats.

The job people actually want here is "post this into that channel", and Graph makes it a two-level
lookup before you can: a team id, then a channel id, then a message whose text has to be wrapped in
a body object with a content type. ``post`` is that, in one call.

Chats and channels are different endpoints with the same message shape, so both are here and the
shape is built once.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

from .._errors import GraphError
from .._scopes import Scopes
from .base import GraphResource

__all__ = ["Teams"]

DEFAULT_FIELDS = "id,displayName,description"

MESSAGE_FIELDS = "id,createdDateTime,from,body,importance"

_IMPORTANCE = ("normal", "high", "urgent")


def _message(text: str, html: bool, subject: Optional[str], importance: str) -> Dict[str, Any]:
    """The chatMessage shape, which channel posts, replies and chats all share."""
    if not text:
        raise GraphError(0, "invalidRequest", "'message' is required")
    if importance not in _IMPORTANCE:
        raise GraphError(
            0, "invalidRequest", f"'importance' must be one of {', '.join(_IMPORTANCE)}"
        )

    payload: Dict[str, Any] = {
        "body": {"contentType": "html" if html else "text", "content": text},
    }
    if subject:
        payload["subject"] = subject
    if importance != "normal":
        payload["importance"] = importance
    return payload


class Teams(GraphResource):
    """Teams, their channels, and the messages in both.

    Delegated access only in practice. Reading channel messages with *application* permissions is
    one of Graph's protected APIs: Microsoft has to approve the app first, and until they do the
    call returns 403 no matter what consent the tenant has granted. Posting as a user works
    normally.
    """

    path = "/teams"
    scopes = Scopes.combine(Scopes.TEAM_READ_BASIC, Scopes.CHANNEL_MESSAGE_SEND)

    # ── finding your way ─────────────────────────────────────────────────────

    def mine(self, select: str = DEFAULT_FIELDS) -> AsyncIterator[Dict[str, Any]]:
        """Teams the signed-in person belongs to."""
        return self._client.paged("/me/joinedTeams", select=select)

    def channels(
        self, team_id: str, select: str = DEFAULT_FIELDS
    ) -> AsyncIterator[Dict[str, Any]]:
        """Channels in a team."""
        return self._client.paged(f"/teams/{team_id}/channels", select=select)

    async def channel_by_name(self, team_id: str, name: str) -> Dict[str, Any]:
        """Find a channel by its display name, so a caller need not carry channel ids around."""
        async for channel in self.channels(team_id):
            if channel.get("displayName", "").casefold() == name.casefold():
                return channel
        raise GraphError(0, "itemNotFound", f"team '{team_id}' has no channel named '{name}'")

    def members(self, team_id: str) -> AsyncIterator[Dict[str, Any]]:
        """Who is in a team."""
        return self._client.paged(f"/teams/{team_id}/members")

    # ── channel messages ─────────────────────────────────────────────────────

    async def post(
        self,
        team_id: str,
        channel_id: str,
        message: str,
        html: bool = False,
        subject: Optional[str] = None,
        importance: str = "normal",
    ) -> Dict[str, Any]:
        """Post a message to a channel. Returns the created message, including its id."""
        return await self._client.post(
            f"/teams/{team_id}/channels/{channel_id}/messages",
            body=_message(message, html, subject, importance),
        )

    async def reply(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        message: str,
        html: bool = False,
    ) -> Dict[str, Any]:
        """Reply in an existing channel thread.

        Graph has no "reply to a reply": every reply attaches to the thread's root message, so
        ``message_id`` is the id ``post`` returned.
        """
        return await self._client.post(
            f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
            body=_message(message, html, None, "normal"),
        )

    def messages(
        self, team_id: str, channel_id: str, top: int = 50, select: str = MESSAGE_FIELDS
    ) -> AsyncIterator[Dict[str, Any]]:
        """Walk a channel's messages, newest first.

        Replies are not included; they hang off each message's own ``replies`` collection.
        """
        return self._client.paged(
            f"/teams/{team_id}/channels/{channel_id}/messages", select=select, top=top
        )

    # ── chats ────────────────────────────────────────────────────────────────

    def chats(self, select: str = "id,topic,chatType,lastUpdatedDateTime") -> AsyncIterator[Dict[str, Any]]:
        """The signed-in person's chats, most recently updated first."""
        return self._client.paged("/me/chats", select=select, orderby="lastUpdatedDateTime desc")

    async def send_chat(self, chat_id: str, message: str, html: bool = False) -> Dict[str, Any]:
        """Send a message into an existing chat.

        Starting a *new* chat is a different call -- POST ``/chats`` with its members -- and is
        deliberately not wrapped here until something needs it.
        """
        return await self._client.post(
            f"/chats/{chat_id}/messages", body=_message(message, html, None, "normal")
        )
