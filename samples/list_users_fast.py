"""Read the whole directory, then fetch details for many users at once.

Shows where the speed comes from: batching, not asyncio. Two hundred lookups become ten
round-trips rather than two hundred.

    Needs:  User.Read.All as an APPLICATION permission, with admin consent
    Run:    export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
            python samples/list_users_fast.py
"""

import asyncio
import sys
import time

from graphplug import GraphClient, GraphError


async def main() -> int:
    try:
        async with GraphClient.from_env() as graph:

            # One page at a time; nothing buffers the whole directory.
            ids = []
            async for user in graph.paged("/users", select="id,displayName,mail", top=999):
                ids.append(user["id"])
                if len(ids) >= 200:
                    break
            print(f"listed {len(ids)} users")

            if not ids:
                return 0

            # Batched twenty at a time and dispatched concurrently, bounded internally so Graph
            # is not overwhelmed. You never write asyncio.gather yourself.
            started = time.monotonic()
            results = await graph.users_get_many(ids) if hasattr(graph, "users_get_many") else \
                await graph.batch([("GET", f"/users/{i}?$select=id,mail,jobTitle") for i in ids])
            elapsed = time.monotonic() - started

            ok = sum(1 for r in results if 200 <= r["status"] < 300)
            print(f"fetched {ok}/{len(results)} in {elapsed:.2f}s "
                  f"across {(len(ids) + 19) // 20} round-trips")

        return 0

    except GraphError as error:
        print(f"failed: [{error.status} {error.code}] {error.message}", file=sys.stderr)
        if error.code == "consentRequired":
            print("An administrator has not consented to User.Read.All.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
