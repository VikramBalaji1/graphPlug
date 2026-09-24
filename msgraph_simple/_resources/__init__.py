"""Resource layer: the plug-and-play surface over the generic transport."""

from .base import GraphResource
from .calendar import Calendar
from .mail import Mail

__all__ = ["GraphResource", "Calendar", "Mail"]
