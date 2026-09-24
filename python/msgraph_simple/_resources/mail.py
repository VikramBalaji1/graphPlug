"""Mail.

The point of this module is ``send``. Graph's ``sendMail`` payload is the most awkward thing in the
API to hand-build: recipients are objects inside objects, the body carries a content type, and an
attachment is a base64 blob with an ``@odata.type`` discriminator. Roughly twenty lines of nested
JSON become one call.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence, Union

from .._errors import GraphError
from .._scopes import Scopes
from .base import GraphResource

__all__ = ["Mail"]

Recipients = Union[str, Sequence[str], None]


def _recipients(value: Recipients) -> List[Dict[str, Any]]:
    """One address or many, either way in the shape Graph expects."""
    if not value:
        return []
    addresses = [value] if isinstance(value, str) else list(value)
    return [{"emailAddress": {"address": address}} for address in addresses]


def _attachment(path: Union[str, Path]) -> Dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise GraphError(0, "invalidRequest", f"attachment '{source}' does not exist")

    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": source.name,
        "contentType": content_type,
        "contentBytes": base64.b64encode(source.read_bytes()).decode("ascii"),
    }


class Mail(GraphResource):
    """Messages in the signed-in user's mailbox."""

    path = "/me/messages"
    scopes = Scopes.combine(Scopes.MAIL_READ_WRITE, Scopes.MAIL_SEND)

    #: Graph rejects a message above roughly this size; beyond it an upload session is needed.
    MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024

    # ── sending ──────────────────────────────────────────────────────────────

    def compose(
        self,
        to: Recipients,
        subject: str,
        body: str = "",
        cc: Recipients = None,
        bcc: Recipients = None,
        html: bool = False,
        attachments: Optional[Iterable[Union[str, Path]]] = None,
        reply_to: Recipients = None,
    ) -> Dict[str, Any]:
        """Build the message payload without sending it.

        Exposed because it is useful on its own -- for drafts, for batching, and for seeing exactly
        what would go on the wire.
        """
        if not to:
            raise GraphError(0, "invalidRequest", "'to' is required")

        message: Dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": _recipients(to),
        }
        if cc:
            message["ccRecipients"] = _recipients(cc)
        if bcc:
            message["bccRecipients"] = _recipients(bcc)
        if reply_to:
            message["replyTo"] = _recipients(reply_to)

        if attachments:
            built = [_attachment(path) for path in attachments]
            total = sum(len(item["contentBytes"]) for item in built)
            if total > self.MAX_ATTACHMENT_BYTES:
                raise GraphError(
                    0,
                    "invalidRequest",
                    f"attachments total {total} bytes; Graph rejects a message this large. "
                    "Upload to OneDrive and send a link instead.",
                )
            message["attachments"] = built

        return message

    async def send(
        self,
        to: Recipients,
        subject: str,
        body: str = "",
        cc: Recipients = None,
        bcc: Recipients = None,
        html: bool = False,
        attachments: Optional[Iterable[Union[str, Path]]] = None,
        reply_to: Recipients = None,
        save_to_sent: bool = True,
    ) -> None:
        """Send a message. Returns nothing -- Graph answers ``sendMail`` with 202 and no body."""
        await self._collection_action("sendMail", {
            "message": self.compose(to, subject, body, cc, bcc, html, attachments, reply_to),
            "saveToSentItems": save_to_sent,
        })

    async def send_many(self, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Send many messages in as few round-trips as Graph allows.

        Each entry is the keyword arguments ``send`` takes. Twenty go per round-trip, and the
        batches run concurrently. Per-message failures come back in the results rather than
        raising, so check each ``status``.
        """
        requests = []
        for fields in messages:
            fields = dict(fields)
            save = fields.pop("save_to_sent", True)
            requests.append({
                "method": "POST",
                "url": "/me/sendMail",
                "headers": {"Content-Type": "application/json"},
                "body": {"message": self.compose(**fields), "saveToSentItems": save},
            })
        return await self._client.batch(requests)

    # ── replying ─────────────────────────────────────────────────────────────

    async def reply(self, message_id: str, comment: str = "", reply_all: bool = False) -> None:
        await self._action(message_id, "replyAll" if reply_all else "reply", {"comment": comment})

    async def forward(self, message_id: str, to: Recipients, comment: str = "") -> None:
        await self._action(message_id, "forward", {
            "comment": comment,
            "toRecipients": _recipients(to),
        })

    # ── reading ──────────────────────────────────────────────────────────────

    def inbox(
        self,
        unread_only: bool = False,
        since: Optional[datetime] = None,
        search: Optional[str] = None,
        top: int = 50,
        select: str = "id,subject,from,receivedDateTime,isRead,hasAttachments",
    ) -> AsyncIterator[Dict[str, Any]]:
        """Walk the inbox newest first, narrowed however you like."""
        options: Dict[str, Any] = {"select": select, "top": top}

        filters = []
        if unread_only:
            filters.append("isRead eq false")
        if since is not None:
            moment = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            filters.append(f"receivedDateTime ge {moment.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
        if filters:
            options["filter"] = " and ".join(filters)

        if search:
            # Graph forbids $search with $filter or $orderby, so search wins and the rest goes.
            options = {"select": select, "top": top, "search": f'"{search}"'}
        else:
            options["orderby"] = "receivedDateTime desc"

        return self._client.paged("/me/mailFolders/inbox/messages", **options)

    async def mark_read(self, message_id: str, read: bool = True) -> Dict[str, Any]:
        return await self.update(message_id, {"isRead": read})

    async def move(self, message_id: str, folder: str) -> Dict[str, Any]:
        """Move to a named well-known folder, or to a folder id."""
        return await self._action(message_id, "move", {"destinationId": folder})

    async def delete_many(self, message_ids: Sequence[str]) -> List[Dict[str, Any]]:
        requests = [{"method": "DELETE", "url": f"{self.path}/{i}"} for i in message_ids]
        return await self._client.batch(requests)
