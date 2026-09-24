"""Upload a file, get a link for it, and post that link into a Teams channel.

The three new resources doing one job between them: files, teams and users.

    Needs:  as DELEGATED permissions, on a public client registration --
            Files.ReadWrite, Team.ReadBasic.All, ChannelMessage.Send, User.Read
    Run:    export AZURE_TENANT_ID=... AZURE_CLIENT_ID=...
            python samples/share_report.py report.pdf "Engineering" "deploys"
"""

import asyncio
import os
import sys
from pathlib import Path

from msgraph_simple import GraphClient, GraphError, Scopes

SCOPES = Scopes.combine(
    Scopes.FILES_READ_WRITE,
    Scopes.TEAM_READ_BASIC,
    Scopes.CHANNEL_MESSAGE_SEND,
    Scopes.USER_READ,
)


async def main(local_file: str, team_name: str, channel_name: str) -> int:
    tenant, client = os.environ.get("AZURE_TENANT_ID"), os.environ.get("AZURE_CLIENT_ID")
    if not tenant or not client:
        print("set AZURE_TENANT_ID and AZURE_CLIENT_ID", file=sys.stderr)
        return 2

    source = Path(local_file)
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 2

    try:
        graph = await GraphClient.device_code(tenant, client, SCOPES)
    except GraphError as error:
        print(f"sign-in failed: [{error.code}] {error.message}", file=sys.stderr)
        return 1

    async with graph:
        try:
            me = await graph.users.me()

            # Above 4 MiB this becomes a resumable session on its own. Same call either way.
            await graph.files.upload(source, to=f"/Shared Reports/{source.name}")
            link = await graph.files.share_link(
                f"/Shared Reports/{source.name}", kind="view", scope="organization"
            )
            print(f"uploaded, link: {link}")

            team = await find_team(graph, team_name)
            channel = await graph.teams.channel_by_name(team["id"], channel_name)

            await graph.teams.post(
                team["id"],
                channel["id"],
                f'<b>{source.name}</b> from {me["displayName"]} — <a href="{link}">open</a>',
                html=True,
                subject=f"Report: {source.name}",
            )
            print(f'posted to {team["displayName"]} / {channel["displayName"]}')

        except GraphError as error:
            print(f"failed: [{error.status} {error.code}] {error.message}", file=sys.stderr)
            if error.request_id:
                print(f"request id: {error.request_id}", file=sys.stderr)
            return 1

    return 0


async def find_team(graph: GraphClient, name: str) -> dict:
    async for team in graph.teams.mine():
        if team.get("displayName", "").casefold() == name.casefold():
            return team
    raise GraphError(0, "itemNotFound", f"you are not in a team called '{name}'")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: python samples/share_report.py <file> <team> <channel>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3])))
