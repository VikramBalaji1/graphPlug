"""Mail.

The point of this module is ``send``. Graph's ``sendMail`` payload is the most awkward thing in the
API to hand-build: recipients are objects inside objects, the body carries a content type, and an
attachment is a base64 blob with an ``@odata.type`` discriminator. Roughly twenty lines of nested
JSON become one call.
"""

from __future__ import annotations

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
    """Messages in a mailbox: the signed-in person's, or ``user``'s.

    Every method takes ``user`` (an id or mail address). Under application access there is no
    signed-in person, so pass it: ``send(..., user="reports@contoso.com")`` sends as that mailbox.
    """

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
        user: Optional[str] = None,
    ) -> None:
        """Send a message, from ``user``'s mailbox when given.

        Returns nothing -- Graph answers ``sendMail`` with 202 and no body.
        """
        await self._collection_action("sendMail", {
            "message": self.compose(to, subject, body, cc, bcc, html, attachments, reply_to),
            "saveToSentItems": save_to_sent,
        }, user=user)

    async def send_many(
        self, messages: Sequence[Dict[str, Any]], user: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Send many messages in as few round-trips as Graph allows.

        Each entry is the keyword arguments ``send`` takes, including its own ``user`` to send one
        message from a different mailbox; ``user`` here is the default for the rest. Twenty go per
        round-trip, and the batches run concurrently. Per-message failures come back in the
        results rather than raising, so check each ``status``.
        """
        requests = []
        for fields in messages:
            fields = dict(fields)
            save = fields.pop("save_to_sent", True)
            sender = fields.pop("user", user)
            requests.append({
                "method": "POST",
                "url": self._for("/me/sendMail", sender),
                "headers": {"Content-Type": "application/json"},
                "body": {"message": self.compose(**fields), "saveToSentItems": save},
            })
        return await self._client.batch(requests)

    # ── replying ─────────────────────────────────────────────────────────────

    async def reply(
        self, message_id: str, comment: str = "", reply_all: bool = False,
        user: Optional[str] = None,
    ) -> None:
        action = "replyAll" if reply_all else "reply"
        await self._action(message_id, action, {"comment": comment}, user=user)

    async def forward(
        self, message_id: str, to: Recipients, comment: str = "", user: Optional[str] = None
    ) -> None:
        await self._action(message_id, "forward", {
            "comment": comment,
            "toRecipients": _recipients(to),
        }, user=user)

    # ── reading ──────────────────────────────────────────────────────────────

    def inbox(
        self,
        unread_only: bool = False,
        since: Optional[datetime] = None,
        search: Optional[str] = None,
        top: int = 50,
        select: str = "id,subject,from,receivedDateTime,isRead,hasAttachments",
        user: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Walk the inbox newest first, narrowed however you like."""
        options: Dict[str, Any] = {"select": select, "top": top}

        # Graph refuses $orderby on a property that does not also lead $filter (InefficientFilter),
        # so receivedDateTime comes first, and is bounded by nothing when there is no `since`.
        filters = []
        if since is not None:
            moment = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            filters.append(f"receivedDateTime ge {moment.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
        if unread_only:
            if not filters:
                filters.append("receivedDateTime ge 1900-01-01T00:00:00Z")
            filters.append("isRead eq false")
        if filters:
            options["filter"] = " and ".join(filters)

        if search:
            # Graph forbids $search with $filter or $orderby, so search wins and the rest goes.
            options = {"select": select, "top": top, "search": f'"{search}"'}
        else:
            options["orderby"] = "receivedDateTime desc"

        return self._client.paged(self._for("/me/mailFolders/inbox/messages", user), **options)

    async def mark_read(
        self, message_id: str, read: bool = True, user: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self.update(message_id, {"isRead": read}, user=user)

    async def move(self, message_id: str, folder: str, user: Optional[str] = None) -> Dict[str, Any]:
        """Move to a named well-known folder, or to a folder id."""
        return await self._action(message_id, "move", {"destinationId": folder}, user=user)

    async def delete_many(
        self, message_ids: Sequence[str], user: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        requests = [{"method": "DELETE", "url": self._item(i, user)} for i in message_ids]
        return await self._client.batch(requests)
