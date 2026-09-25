"""Named Graph permissions.

Plug and play means not having to know that sending mail needs ``Mail.Send``. Each resource
declares the scopes it uses, so a client can be built from the resources you intend to touch and a
missing permission produces an error that names what to grant.
"""

from __future__ import annotations

from typing import Sequence, Tuple

__all__ = ["Scopes"]


class Scopes:
    """Delegated permission names, grouped by what you are trying to do."""

    # Identity
    USER_READ: Tuple[str, ...] = ("User.Read",)
    USER_READ_ALL: Tuple[str, ...] = ("User.Read.All",)

    # Mail
    MAIL_READ: Tuple[str, ...] = ("Mail.Read",)
    MAIL_READ_WRITE: Tuple[str, ...] = ("Mail.ReadWrite",)
    MAIL_SEND: Tuple[str, ...] = ("Mail.Send",)

    # Calendar
    CALENDARS_READ: Tuple[str, ...] = ("Calendars.Read",)
    CALENDARS_READ_WRITE: Tuple[str, ...] = ("Calendars.ReadWrite",)

    # Files
    FILES_READ: Tuple[str, ...] = ("Files.Read",)
    FILES_READ_WRITE: Tuple[str, ...] = ("Files.ReadWrite",)
    #: Other people's drives and SharePoint document libraries, not just your own.
    FILES_READ_WRITE_ALL: Tuple[str, ...] = ("Files.ReadWrite.All",)

    # Teams
    TEAM_READ_BASIC: Tuple[str, ...] = ("Team.ReadBasic.All",)
    CHANNEL_MESSAGE_SEND: Tuple[str, ...] = ("ChannelMessage.Send",)
    #: Reading channel messages is a protected API: Microsoft must approve the app first.
    CHANNEL_MESSAGE_READ: Tuple[str, ...] = ("ChannelMessage.Read.All",)
    CHAT_READ_WRITE: Tuple[str, ...] = ("Chat.ReadWrite",)

    #: Everything the built-in resources can use. Convenient for a first run; narrow it afterwards.
    EVERYTHING: Tuple[str, ...] = (
        "User.Read", "User.Read.All",
        "Mail.ReadWrite", "Mail.Send",
        "Calendars.ReadWrite",
        "Files.ReadWrite",
        "Team.ReadBasic.All", "ChannelMessage.Send", "Chat.ReadWrite",
    )

    @staticmethod
    def combine(*groups: Sequence[str]) -> Tuple[str, ...]:
        """Merge scope groups, keeping order and dropping duplicates."""
        return tuple(dict.fromkeys(scope for group in groups for scope in group))
