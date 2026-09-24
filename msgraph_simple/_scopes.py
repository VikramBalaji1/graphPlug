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

    #: Everything the built-in resources can use. Convenient for a first run; narrow it afterwards.
    EVERYTHING: Tuple[str, ...] = (
        "User.Read", "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite",
    )

    @staticmethod
    def combine(*groups: Sequence[str]) -> Tuple[str, ...]:
        """Merge scope groups, keeping order and dropping duplicates."""
        seen: dict = {}
        for group in groups:
            for scope in group:
                seen[scope] = None
        return tuple(seen)
