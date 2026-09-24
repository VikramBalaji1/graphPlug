# msgraph_simple

Plug-and-play Microsoft Graph for Python. Sending a mail is one call. Booking a Teams meeting is
one call. Authentication, retries, throttling, paging, batching and large file transfers happen
underneath.

```python
import asyncio
from msgraph_simple import GraphClient, Scopes

async def main():
    graph = await GraphClient.device_code(TENANT, CLIENT, Scopes.MAIL_SEND)

    async with graph:
        await graph.mail.send(
            to="alice@contoso.com",
            subject="Quarterly report",
            body="<p>Attached.</p>", html=True,
            attachments=["report.pdf"],
        )

asyncio.run(main())
```

> **Start here:** [USAGE.md](USAGE.md) is the full walkthrough. When something fails, see
> [docs/troubleshooting.md](docs/troubleshooting.md). For working code, see [samples/](samples).
> [ARCHITECTURE.md](ARCHITECTURE.md) explains *why* the rules are what they are.

---

## Install

```bash
pip install msgraph-simple
```

Two dependencies, both Microsoft's own — `azure-identity` for credentials, `msgraph-core` for the
supported middleware pipeline. Pure Python, so it installs anywhere.

`msgraph-sdk` is deliberately not used: its dependency tree does not resolve in practice, hanging
`pip` and `uv` indefinitely. `msgraph-core` resolves in a few seconds and carries the parts that
matter.

---

## What you get

**A resource layer, over five areas.** Graph's `sendMail` payload is roughly twenty lines of
nested JSON — recipients as objects inside objects, a body with a content type, attachments
base64-encoded with an `@odata.type` discriminator. A Teams meeting needs `isOnlineMeeting` *and*
`onlineMeetingProvider`. A drive item is `/me/drive/root:/reports/q3.xlsx:` — with a closing colon
everybody forgets. A user search returns a bare 400 without a `ConsistencyLevel` header. All of
that is built for you.

```python
await graph.mail.send(to=..., subject=..., body=..., attachments=[...])
async for message in graph.mail.inbox(unread_only=True): ...

event = await graph.calendar.schedule(subject=..., start=..., end=..., online=True)
print(event["onlineMeeting"]["joinUrl"])

await graph.files.upload("q3.xlsx", to="/reports/2026/q3.xlsx")
url = await graph.files.share_link("/reports/2026/q3.xlsx", kind="edit")

channel = await graph.teams.channel_by_name(team_id, "deploys")
await graph.teams.post(team_id, channel["id"], f"Report is up: {url}")

async for person in graph.users.find("smith"): ...
boss = await graph.users.manager()
```

**Everything generic, too.** `get`, `post`, `patch`, `delete`, `paged`, `batch`, `download`,
`upload` — every `v1.0` and `beta` endpoint reachable without waiting for a typed wrapper.

**Speed that does not need orchestrating.** Batching is the lever, not asyncio:

| | 500 user lookups |
|---|---|
| One at a time | 500 round-trips |
| `graph.batch(...)` | **25 round-trips** |
| …dispatched concurrently | **~5 round-trip times** |

`batch`, `get_many` and `send_many` chunk at Graph's limit of 20 and dispatch under a bounded
semaphore. You never write `asyncio.gather`, and you do not get throttled for going too wide.

**Adding a resource is one subclass.** `list`, `get`, `create`, `update`, `delete` and `get_many`
come from a shared base; a new resource sets a path and adds whatever is specific to it. The five
that ship are each about 150 lines and are worth reading as worked examples.

---

## The two access models

Choosing wrong is how a script ends up with far more reach than intended.

| | **Application-level** | **Delegated** |
|---|---|---|
| Acting as | The application itself | A signed-in person |
| Reach | **The whole tenant** | Only what that person can already do |
| `graph.mail` / `graph.calendar` | No — there is no user | Yes |
| Human needed | No | Yes, at first sign-in |
| Constructor | `app_only`, `from_env` | `device_code`, `interactive` |

`Mail.Read` as an **application** permission reads every mailbox in the tenant. The same name as a
**delegated** permission reads only the signed-in person's mail.

Supported sign-ins: client secret, device code, and authorization code with PKCE. Certificate,
managed identity and on-behalf-of are a credential swap away — `azure-identity` ships them all and
nothing here constrains which you pass.

---

## Security properties

Deliberate, and tested rather than documented and hoped for.

- **Delegated scopes are never defaulted.** `.default` on a delegated flow silently requests every
  scope ever consented for that client. Omitting scopes is an error naming the field.
- **A caller-supplied `Authorization` header is rejected** before the request leaves.
- **Response headers pass an allowlist, never a denylist.** A denylist fails open on whatever
  header Microsoft adds tomorrow.
- **The PKCE verifier never leaves the process**, and `state` is validated internally so the CSRF
  check cannot be skipped.
- **The bearer token is withheld from any host but `graph.microsoft.com`.** Pre-authenticated
  download URLs still work; they simply travel unauthenticated.
- **Nothing is written to disk.** The token cache is in memory for the life of the client.

---

## Errors

One exception type carrying data, rather than a hierarchy.

```python
except GraphError as e:
    e.status        # HTTP status, or 0 when there was no response at all
    e.code          # "itemNotFound", or a core code such as "consentRequired"
    e.message
    e.request_id    # quote this to Microsoft support
    e.retry_after
    e.inner         # Graph's own inner error, verbatim
```

Throttling is handled for you — the pipeline honours `Retry-After`. Catching
`activityLimitReached` means the retry budget ran out, and `retry_after` tells you how long to
wait. Every code and its fix is in [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Logging

Off unless asked. `MSGRAPH_LOG_LEVEL=info` or `=error`; one JSON object per line on stderr.

```
{"level":"info","event":"request","method":"GET","url":"https://graph.microsoft.com/v1.0/users","status":200,"ms":214,"requestId":"a1b2c3d4","errorCode":null}
```

URLs are logged **without their query string**, because an OData `$filter` routinely carries email
addresses. Headers, bodies and credential material are never logged.

---

## Development

```bash
pip install -e .
python -m unittest discover -s tests     # 151 tests
python -m build --wheel
```

No container, no compiler, no platform-specific build.

---

## Status

The package is complete and tested. **151 tests**, covering the middleware contract, request
construction, paging, batching, file round-trips, the exact paths and payloads all five resources
build, the error taxonomy, concurrency bounds, the sign-in orchestration, the drive addressing
rules and the logger.

### What still needs a tenant

Everything above is verified without one. These cannot be:

- Whether each sign-in flow completes against real Entra.
- Whether the permissions each resource declares are sufficient in practice.

Set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` and the live checks become
available.

### History

An earlier version of this package was a C# core compiled to a native library and reached through
a C ABI. It has been removed. Most of the rules here — the header allowlist, the error taxonomy, the
upload thresholds, the batch chunking, the two access models — were worked out there and survived
the rewrite unchanged, which is decent evidence they were about Graph rather than about C#.
[ARCHITECTURE.md](ARCHITECTURE.md) documents the current design and records the reasoning.

---

## Licence

[MIT](LICENSE). `azure-identity` and `msgraph-core` are MIT too, so nothing here carries an
obligation you did not choose.
