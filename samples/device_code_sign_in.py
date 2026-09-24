"""Sign a real person in from a headless machine, then act as them.

Delegated access: the effective permissions are the intersection of the scopes you ask for and
what that person can already do. /me works here, and only their own mail is reachable.

    Needs:  a PUBLIC client registration (no secret) with "Allow public client flows" = Yes,
            and the delegated permissions below granted
    Run:    export AZURE_TENANT_ID=... AZURE_CLIENT_ID=...
            python samples/device_code_sign_in.py
"""

import os
import sys

from msgraph_simple import GraphClient, GraphError

SCOPES = ["User.Read", "Mail.Read"]


def main() -> int:
    tenant = os.environ.get("AZURE_TENANT_ID")
    client = os.environ.get("AZURE_CLIENT_ID")
    if not tenant or not client:
        print("set AZURE_TENANT_ID and AZURE_CLIENT_ID", file=sys.stderr)
        return 2

    try:
        # Two-phase, so the prompt is yours to render. device_code() does this for you and just
        # prints Microsoft's own instruction text.
        flow = GraphClient.begin_device_code(tenant, client, SCOPES)

        print("=" * 64)
        print(f"  Open {flow.verification_uri}")
        print(f"  Enter the code:  {flow.user_code}")
        print(f"  It expires in {flow.expires_in // 60} minutes.")
        print("=" * 64)

        # Blocks until they finish. Cancel the flow if anything goes wrong so the background
        # poll does not outlive this process.
        graph = flow.complete()

    except KeyboardInterrupt:
        print("\ncancelled.", file=sys.stderr)
        flow.cancel()
        return 130
    except GraphError as error:
        print(f"sign-in failed: [{error.code}] {error.message}", file=sys.stderr)
        return 1

    with graph:
        me = graph.get("/me")
        print(f"\nSigned in as {me['displayName']} <{me.get('mail') or me['userPrincipalName']}>")

        # Delegated access means this reads only their mailbox, never anyone else's.
        recent = graph.get("/me/messages", select="subject,receivedDateTime", top=5)
        for message in recent.get("value", []):
            print(f"  {message['receivedDateTime'][:10]}  {message['subject']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
