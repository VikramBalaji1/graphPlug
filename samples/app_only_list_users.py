"""List every user in the tenant, using application-level access.

The app acts as itself, with the application permissions an administrator consented to. There is
no signed-in person, so /me does not work here.

    Needs:  User.Read.All as an APPLICATION permission, with admin consent
    Run:    export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
            python samples/app_only_list_users.py
"""

import sys

from msgraph_simple import GraphClient, GraphError


def main() -> int:
    try:
        # Reads the three AZURE_* variables. The secret is passed across the boundary once and
        # lives thereafter only inside the credential the core holds.
        with GraphClient.from_env() as graph:
            count = 0
            # paged() is a generator: it fetches one page at a time and stops when you do.
            for user in graph.paged("/users", select="id,displayName,mail", top=999):
                print(f"{user.get('displayName', '?'):<40} {user.get('mail') or '(no mail)'}")
                count += 1

            print(f"\n{count} users.")
        return 0

    except GraphError as error:
        # One exception type; branch on status and code rather than on a class tree.
        print(f"failed: [{error.status} {error.code}] {error.message}", file=sys.stderr)
        if error.request_id:
            print(f"request id (quote this to Microsoft support): {error.request_id}",
                  file=sys.stderr)
        if error.code == "consentRequired":
            print("An administrator has not consented to User.Read.All.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
