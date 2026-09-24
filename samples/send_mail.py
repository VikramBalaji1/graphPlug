"""Send a mail, with an attachment, as a signed-in person.

    Needs:  Mail.Send as a DELEGATED permission, on a public client registration
    Run:    export AZURE_TENANT_ID=... AZURE_CLIENT_ID=...
            python samples/send_mail.py someone@contoso.com
"""

import asyncio
import os
import sys

from msgraph_simple import GraphClient, GraphError, Scopes


async def main(recipient: str) -> int:
    tenant, client = os.environ.get("AZURE_TENANT_ID"), os.environ.get("AZURE_CLIENT_ID")
    if not tenant or not client:
        print("set AZURE_TENANT_ID and AZURE_CLIENT_ID", file=sys.stderr)
        return 2

    try:
        # Prints a code, waits for the person to sign in on any device.
        graph = await GraphClient.device_code(tenant, client, Scopes.MAIL_SEND)
    except GraphError as error:
        print(f"sign-in failed: [{error.code}] {error.message}", file=sys.stderr)
        return 1

    async with graph:
        try:
            await graph.mail.send(
                to=recipient,
                subject="Hello from msgraph_simple",
                body="<p>Sent with <b>one call</b>.</p>",
                html=True,
                attachments=[__file__],      # this script, as a demonstration
            )
            print(f"sent to {recipient}")
        except GraphError as error:
            print(f"failed: [{error.status} {error.code}] {error.message}", file=sys.stderr)
            if error.request_id:
                print(f"request id: {error.request_id}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python samples/send_mail.py <recipient>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
