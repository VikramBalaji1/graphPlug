"""Resource layer: the plug-and-play surface over the generic transport."""

from .base import GraphResource
from .calendar import Calendar
from .files import Files
from .mail import Mail
from .teams import Teams
from .users import Users

__all__ = ["GraphResource", "Calendar", "Files", "Mail", "Teams", "Users"]
