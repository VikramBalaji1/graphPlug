# Using graphplug from Python

Import one class, hand it credentials, call Graph. Sending a mail is one call; booking a Teams
meeting is one call. Token refresh, retry, throttling, paging, batching and large file transfers
happen underneath.

```python
import asyncio
from graphplug import GraphClient, Scopes

async def main():
    async with GraphClient.from_env() as graph:
        await graph.mail.send(to="alice@contoso.com", subject="Hi", body="Hello")

asyncio.run(main())
```

Everything is `async`. The one piece of ceremony is `asyncio.run(main())` at the top of a script;
in exchange, concurrency is handled for you and never has to be written by hand.

---

## Contents

- [Install](#install)
- [Pick an access model](#pick-an-access-model) — the one decision that matters
- [Signing in](#signing-in) — including any credential of your own
- [Mail](#mail)
- [Calendar and meetings](#calendar-and-meetings)
- [Files](#files)
- [Teams and chat](#teams-and-chat)
- [People and the directory](#people-and-the-directory)
- [Generic requests](#generic-requests)
- [Paging](#paging)
- [Batching, and where the speed comes from](#batching-and-where-the-speed-comes-from)
- [Errors](#errors)
- [Logging](#logging)
- [Concurrency and lifetime](#concurrency-and-lifetime)
- [Adding a resource of your own](#adding-a-resource-of-your-own)
- [Full reference](#full-reference)
- [Things that will catch you out](#things-that-will-catch-you-out)

---

## Install

```bash
pip install graphplug
```

Pure Python, and it installs anywhere. Two dependencies, both Microsoft's own: `azure-identity`
for credentials and `msgraph-core` for the supported middleware pipeline.

> `msgraph-sdk` is deliberately not used. Its dependency tree does not resolve in practice — `pip`
> and `uv` both hang on it. `msgraph-core` resolves in a few seconds.

---

## Pick an access model

The only decision with real consequences. Get it wrong and a script has far more reach than you
intended.

| | **Application-level** | **Delegated** |
|---|---|---|
| Acting as | The application itself | A signed-in person |
| Reach | **The whole tenant** | Only what that person can already do |
| `/me`, `graph.mail`, `graph.calendar` | No — there is no user | Yes |
| Human needed | No | Yes, at first sign-in |
| Entra registration | Confidential client, holds a secret | Public client, holds none |
| Constructor | `app_only`, `from_env` | `device_code`, `interactive` |

`Mail.Read` as an **application** permission reads every mailbox in the tenant. The same name as a
**delegated** permission reads only the signed-in person's mail.

The `graph.mail` and `graph.calendar` resources address `/me`, so they need **delegated** access.

---

## Signing in

### Application-level, from the environment

```python
async with GraphClient.from_env() as graph:          # AZURE_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET
    users = await graph.get("/users", select="id,mail")
```

Or explicitly:

```python
graph = GraphClient.app_only(
    tenant_id="contoso.onmicrosoft.com",
    client_id="...",
    client_secret="...",
    max_concurrency=12,          # see "Concurrency" below
)
```

Building a client contacts nothing. The first token is fetched on the first request, so a bad
secret surfaces then rather than at construction.

### Any other credential

Managed identity, a certificate, on-behalf-of, a chained credential — anything `azure-identity`
ships, and anything of your own with a `get_token`:

```python
from azure.identity.aio import ManagedIdentityCredential
graph = GraphClient.from_credential(ManagedIdentityCredential())

from azure.identity.aio import CertificateCredential
graph = GraphClient.from_credential(
    CertificateCredential(TENANT, CLIENT, certificate_path="app.pem")
)
```

Nothing above the transport knows how the token was obtained, so these need no support here and
behave identically once built.

A **synchronous** credential works too and is run on a worker thread. Every flow has both
spellings in `azure-identity` (`azure.identity` and `azure.identity.aio`), the async one is easy to
miss, and without this the mistake would surface at the first request as an error about an
un-awaited coroutine rather than about the credential.

Scopes default to `.default`, which is what application-level credentials want. Pass `scopes=` for
anything delegated.

---

### Delegated, device code

For headless boxes and containers — the person signs in on any other device.

```python
graph = await GraphClient.device_code(tenant_id, client_id, Scopes.MAIL_SEND)
```

To render your own prompt:

```python
flow = await GraphClient.begin_device_code(tenant_id, client_id, Scopes.MAIL_READ)
print(f"Go to {flow.verification_uri} and enter {flow.user_code}")
print(f"Expires in {flow.expires_in // 60} minutes.")

graph = await flow.complete()        # blocks until they finish
# await flow.cancel()                # if you give up instead
```

### Delegated, browser

```python
graph = await GraphClient.interactive(
    tenant_id, client_id,
    scopes=Scopes.combine(Scopes.MAIL_SEND, Scopes.CALENDARS_READ_WRITE),
    redirect_uri="http://localhost:8400",
)
```

Opens a browser, listens on loopback for exactly one redirect, validates the anti-forgery value
internally, and exchanges the code using PKCE. The verifier never leaves the process.

### Scopes

You do not have to remember that sending mail needs `Mail.Send`:

```python
from graphplug import Scopes

Scopes.MAIL_SEND                 # ("Mail.Send",)
Scopes.CALENDARS_READ_WRITE
Scopes.combine(Scopes.MAIL_SEND, Scopes.CALENDARS_READ_WRITE)
Scopes.EVERYTHING                # handy for a first run; narrow it afterwards
```

Each resource declares what it needs: `graph.mail.scopes`, `graph.calendar.scopes`.

**Delegated scopes are never guessed.** Omit them and you get an error naming the field. There is
no safe default — `.default` on a delegated flow silently requests every scope ever consented for
that client.

---

## Mail

### Sending

```python
await graph.mail.send(
    to="alice@contoso.com",              # or a list
    subject="Quarterly report",
    body="<p>Attached.</p>",
    html=True,
    cc=["bob@contoso.com"],
    bcc=None,
    attachments=["report.pdf"],
    save_to_sent=True,
)
```

That one call builds Graph's `sendMail` payload: recipients as nested objects, the body with its
content type, and each attachment base64-encoded with its `@odata.type` discriminator.

To see the payload without sending, or to build a draft:

```python
message = graph.mail.compose(to="a@x.com", subject="Hi", body="Hello")
```

Attachments above roughly 3 MB are refused with advice — Graph rejects the whole message, so the
answer is to upload to OneDrive and send a link.

### Sending many

```python
results = await graph.mail.send_many([
    {"to": person["mail"], "subject": "Welcome", "body": greeting(person)}
    for person in people
])
```

Twenty per round-trip, batches dispatched concurrently. Failures come back **in the results**, so
check each `status`.

### Reading

```python
async for message in graph.mail.inbox(unread_only=True):
    print(message["receivedDateTime"], message["subject"])

async for message in graph.mail.inbox(since=datetime.now(timezone.utc) - timedelta(days=7)):
    ...

async for message in graph.mail.inbox(search="invoice"):
    ...
```

Newest first. `search` replaces the filter and ordering, because Graph forbids combining them.

### Acting on a message

```python
await graph.mail.reply(message_id, comment="Thanks", reply_all=False)
await graph.mail.forward(message_id, to="b@x.com", comment="FYI")
await graph.mail.mark_read(message_id)
await graph.mail.move(message_id, folder="archive")
await graph.mail.delete_many([id_a, id_b, id_c])       # batched
```

---

## Calendar and meetings

### Booking

```python
event = await graph.calendar.schedule(
    subject="Project sync",
    start=datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
    end=datetime(2026, 3, 2, 9, 30, tzinfo=timezone.utc),
    attendees=["alice@contoso.com"],
    optional_attendees=["bob@contoso.com"],
    online=True,                  # adds the Teams join link
    location="Room 3",
    body="Agenda: progress, blockers.",
    reminder_minutes=15,
)

print(event["onlineMeeting"]["joinUrl"])
```

`online=True` sets both `isOnlineMeeting` **and** `onlineMeetingProvider` — the first alone
produces an event with no join link, which is a common and confusing mistake. Times become the
`dateTime`/`timeZone` pairs Graph wants; a timezone-aware value is converted to UTC, and a naive
one keeps the `timezone_name` you pass.

### Finding a slot first

```python
suggestions = await graph.calendar.find_times(
    ["alice@contoso.com", "bob@contoso.com"], duration_minutes=30, within_days=5
)
best = suggestions[0]["meetingTimeSlot"]
```

Or look at raw availability:

```python
await graph.calendar.free_busy(["alice@contoso.com"], start, end, interval_minutes=30)
```

### Reading and responding

```python
async for event in graph.calendar.upcoming(days=7):
    print(event["start"]["dateTime"], event["subject"])

await graph.calendar.respond(event_id, "accept", comment="See you")   # or decline / tentativelyAccept
await graph.calendar.cancel(event_id, comment="Clashes")
```

`upcoming` uses `calendarView`, which expands recurring series. Listing `/me/events` returns the
series master instead of its occurrences — rarely what you want.

---

## Files

`graph.files` works on the signed-in person's OneDrive. Every method takes **either** a drive path
(leading slash) **or** an item id — that one rule replaces Graph's colon syntax, which is the part
everybody gets wrong:

```text
by path   /reports/q3.xlsx   ->  /me/drive/root:/reports/q3.xlsx:
by id     01ABCDEF...        ->  /me/drive/items/01ABCDEF...
```

Note the *closing* colon. Leaving it off produces a 400 that mentions nothing about colons.

```python
await graph.files.upload("q3.xlsx")                          # to the drive root
await graph.files.upload("q3.xlsx", to="/reports/2026/q3.xlsx")
await graph.files.download("/reports/2026/q3.xlsx", "local.xlsx")

async for item in graph.files.folder("/reports"):            # direct children
    print(item["name"], item["size"])

async for hit in graph.files.search("quarterly"):            # name and content
    print(hit["webUrl"])

await graph.files.make_folder("/reports/2027")
info = await graph.files.metadata("/reports/2026/q3.xlsx")
await graph.files.remove("/reports/old.xlsx")                # to the recycle bin

url = await graph.files.share_link("/reports/2026/q3.xlsx", kind="edit")
```

`upload` replaces whatever is at the destination and switches to a resumable session above 4 MiB on
its own — the path you write is the same either way.

`share_link` takes `kind` of `view`, `edit` or `embed` and `scope` of `anonymous`, `organization`
or `users`, and returns just the URL. `anonymous` is frequently disabled by tenant policy, which
arrives as an `accessDenied` error rather than a working link.

### Any endpoint, not just OneDrive

`graph.files` is a convenience over two generic methods that work against any Graph endpoint
returning or accepting bytes — a message attachment, a SharePoint library, a report export:

```python
result = await graph.download("/me/drive/items/{id}/content", "local.bin")
result["bytesWritten"]

result = await graph.upload("/me/drive/root:/big.zip:/content", "big.zip")
result["bytesSent"]
```

Use these for a drive that is not your own — someone else's, or a SharePoint library — with
`Files.ReadWrite.All`.

Downloads stream to disk; the bytes never enter memory or a JSON envelope. The destination
directory must already exist, and a failed download leaves nothing behind because it writes to a
temporary name and renames only on success.

Uploads switch to a resumable session above 4 MiB automatically, in 10 MiB chunks, resuming from
whatever Graph says it already has.

---

## Teams and chat

```python
async for team in graph.teams.mine():
    print(team["displayName"], team["id"])

channel = await graph.teams.channel_by_name(team_id, "deploys")   # so you need not carry ids

posted = await graph.teams.post(team_id, channel["id"], "Deploy is green")
await graph.teams.reply(team_id, channel["id"], posted["id"], "confirmed in prod")

await graph.teams.post(team_id, channel["id"], "<b>Incident</b>", html=True,
                       subject="SEV-2", importance="urgent")

async for message in graph.teams.messages(team_id, channel["id"]):
    print(message["from"]["user"]["displayName"], message["body"]["content"])

async for chat in graph.teams.chats():
    await graph.teams.send_chat(chat["id"], "on my way")
```

Replies attach to a thread's **root** message, because Graph has no reply-to-a-reply — so the id
you pass `reply` is the one `post` returned.

> **Worth knowing before you plan around it.** *Reading* channel messages with **application**
> permissions is one of Graph's protected APIs: Microsoft has to approve the app first, and until
> they do the call returns 403 whatever consent the tenant has granted. Posting as a signed-in
> person is not affected.

---

## People and the directory

```python
me = await graph.users.me()

async for person in graph.users.find("smith"):        # display name or mail address
    print(person["displayName"], person["mail"])

alice = await graph.users.by_email("alice@contoso.com")
boss = await graph.users.manager()                    # yours, or manager("alice@contoso.com")

async for report in graph.users.reports("alice@contoso.com"):
    print(report["displayName"])

async for group in graph.users.groups():
    print(group["displayName"])

await graph.users.photo("alice.jpg", user="alice@contoso.com", size="96x96")
```

Everywhere a `user` argument is optional, omitting it means the signed-in person — `/me` rather
than `/users/{id}`. Under application-level access there is no signed-in person, so those calls
need an explicit id.

`find` uses Graph's `$search`, which requires a `ConsistencyLevel: eventual` header. The resource
sends it, including on every subsequent page. Without it Graph answers a bare 400 that explains
none of this.

`by_email` is not the same as `get(address)`: `get` resolves the *user principal name*, which is
often but not always the mail address. `by_email` filters on `mail` itself and raises
`itemNotFound` rather than quietly returning the wrong person.

Bulk lookups go through `get_many`, which batches — see
[Batching](#batching-and-where-the-speed-comes-from).

---

## Generic requests

Anything the resources do not cover:

```python
await graph.get("/users/alice@contoso.com")
await graph.post("/teams", body={...})
await graph.patch("/users/{id}", body={"jobTitle": "Engineer"})
await graph.delete("/users/{id}")
```

OData options are plain keywords — `select`, `filter`, `top`, `skip`, `expand`, `orderby`,
`search`, `count`:

```python
await graph.get("/users", select="id,mail", filter="accountEnabled eq true", top=999)
```

Anything else becomes a literal query parameter. You never URL-encode anything.

For the whole envelope, the preview endpoint, or custom headers:

```python
response = await graph.request(
    "GET", "/users",
    version="beta",
    headers={"ConsistencyLevel": "eventual"},
    select="id",
)
response["status"]; response["headers"]; response["body"]; response.get("nextLink")
```

`path` may also be a full URL, in which case `version` and the OData options are ignored because
the URL already carries them. An `Authorization` header is rejected — the client owns that.

---

## Paging

An async generator. One page at a time, stopping when you do.

```python
async for user in graph.paged("/users", select="id,mail", top=999):
    print(user["mail"])
```

Break out early and nothing leaks; the next page is simply never fetched.

---

## Batching, and where the speed comes from

Batching is the big lever, not asyncio:

| | 500 user lookups |
|---|---|
| One request at a time | 500 round-trips |
| `graph.batch(...)` | **25 round-trips** |
| …dispatched concurrently | **~5 round-trip times** |

```python
results = await graph.batch([
    ("GET", "/users"),
    ("GET", "/groups"),
    {"method": "POST", "url": "/users", "body": {...}},
])
```

Split at Graph's limit of 20, dispatched concurrently under the client's concurrency limit, and
returned **in the order you sent them**. A failing sub-request is reported in place:

```python
for index, result in enumerate(results):
    if not 200 <= result["status"] < 300:
        print(index, result["status"], result["body"].get("error"))
```

Every resource also gets `get_many`, which does this for you:

```python
messages = await graph.mail.get_many(message_ids)
```

A `dependsOn` chain must stay within one group of 20 — a dependency spanning a chunk boundary
fails at Graph.

---

## Errors

One exception type carrying data, rather than a hierarchy.

```python
from graphplug import GraphError

try:
    await graph.get("/users/nope")
except GraphError as e:
    e.status        # HTTP status, or 0 when there was no response at all
    e.code          # "itemNotFound", or a package code such as "timeout"
    e.message
    e.request_id    # quote this to Microsoft support
    e.retry_after   # seconds, when Graph said so
    e.inner         # Graph's own inner error, verbatim
```

### Throttling

Usually already handled: the middleware honours `Retry-After` and retries. Catching
`activityLimitReached` means the budget ran out.

```python
except GraphError as e:
    if e.code == "activityLimitReached" and e.retry_after:
        await asyncio.sleep(e.retry_after)
```

Do not substitute a shorter delay — it makes things worse. If you hit this constantly, fetch less:
`select` only the fields you use, raise `top`, and batch related calls.

Every code, its cause and its fix is in [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Logging

Off unless you ask. One JSON object per line on **stderr**.

```bash
GRAPHPLUG_LOG_LEVEL=info python your_script.py
```

```
{"level":"info","event":"request","method":"GET","url":"https://graph.microsoft.com/v1.0/users","status":200,"ms":214,"requestId":"a1b2c3d4","errorCode":null}
```

`error` logs failures only; `info` adds successes. URLs are logged **without their query string**,
because an OData `$filter` routinely carries email addresses, and headers, bodies and credential
material are never logged at all.

---

## Concurrency and lifetime

Concurrency is handled inside the library. `batch`, `get_many` and `send_many` dispatch under a
bounded semaphore, so you get parallelism without writing `asyncio.gather` and without being
throttled for going too wide.

```python
graph = GraphClient.app_only(..., max_concurrency=12)   # the default
```

The default is deliberately modest. Graph throttles per app and per tenant, and mailbox operations
are limited to a handful of concurrent requests per mailbox, so the ceiling is the service's
rather than Python's — going wider earns 429s, not throughput.

If you do orchestrate your own work, the client is safe to use concurrently **within one event
loop**:

```python
async with GraphClient.from_env() as graph:
    results = await asyncio.gather(*(graph.get(f"/users/{i}") for i in ids))
```

`async with` always closes, including when the body raises. Using a closed client raises
`invalidHandle` without touching the network. Closing twice is harmless.

---

## Adding a resource of your own

Every Graph collection shares the same operations over a different path, so a new one is a
subclass and nothing else moves:

```python
from graphplug._resources.base import GraphResource

class Contacts(GraphResource):
    path = "/me/contacts"
    scopes = ("Contacts.ReadWrite",)

    async def add(self, name: str, email: str):
        return await self.create({
            "displayName": name,
            "emailAddresses": [{"address": email, "name": name}],
        })

graph.contacts = Contacts(graph)
```

`mail.py`, `calendar.py`, `files.py`, `teams.py` and `users.py` are all this shape, and each is
worth reading as a worked example.

`list`, `get`, `create`, `update`, `delete` and `get_many` come from the base. `_action` posts to
an action on one item; `_collection_action` posts to one on the collection's owner.

---

## Full reference

### Constructors

| | |
|---|---|
| `GraphClient.app_only(tenant_id, client_id, client_secret, scopes=None, authority_host=None, max_concurrency=12)` | Application-level |
| `GraphClient.from_env(**overrides)` | Application-level from `AZURE_*` |
| `GraphClient.from_credential(credential, scopes=None, max_concurrency=12)` | Any azure-identity credential, sync or async |
| `await GraphClient.device_code(tenant_id, client_id, scopes, authority_host=None)` | Delegated, blocks |
| `await GraphClient.begin_device_code(...)` | Delegated, returns a `PendingSignIn` |
| `await GraphClient.interactive(tenant_id, client_id, scopes, redirect_uri=..., timeout_seconds=900)` | Delegated, browser |

### Client

| | Returns |
|---|---|
| `await request(method, path, version=None, body=None, headers=None, **odata)` | The envelope |
| `await get / post / patch / delete(...)` | The body |
| `paged(path, **odata)` | Async generator of items |
| `await batch(requests)` | List of per-request results, in submission order |
| `await download(path, dest_path)` | `{bytesWritten, destPath, status, headers}` |
| `await upload(path, source_path)` | `{bytesSent, body, status, headers}` |
| `await aclose()` | — |

### Resources

All five inherit `list`, `get`, `create`, `update`, `delete` and `get_many` from `GraphResource`.

**`graph.mail`** — `send`, `send_many`, `compose`, `reply`, `forward`, `inbox`, `mark_read`,
`move`, `delete_many`

**`graph.calendar`** — `schedule`, `schedule_many`, `compose`, `upcoming`, `respond`, `cancel`,
`find_times`, `free_busy`

**`graph.files`** — `upload`, `download`, `folder`, `search`, `metadata`, `make_folder`, `remove`,
`share_link`

**`graph.teams`** — `mine`, `channels`, `channel_by_name`, `members`, `post`, `reply`, `messages`,
`chats`, `send_chat`

**`graph.users`** — `me`, `find`, `by_email`, `manager`, `reports`, `groups`, `photo`

### `GraphError`

`status` · `code` · `message` · `request_id` · `retry_after` · `inner`

### `PendingSignIn`

`user_code` · `verification_uri` · `message` · `authorize_url` · `state` · `expires_in` ·
`await complete()` · `await cancel()`

---

## Things that will catch you out

- **`graph.mail` and `graph.calendar` need delegated access.** They address `/me`, and under
  application access there is no signed-in person.
- **A 403 on something the person can evidently do** is usually scope intersection: delegated
  rights are what you asked for *and* what they already had.
- **Application permissions need admin consent** and are inert without it.
- **`get` returns the body; `request` returns the envelope.** For `nextLink` or the status, use
  `request`.
- **Batch failures are in the results, not in an exception.**
- **`online=True` is what gets you a Teams link**, not `location="Teams"`.
- **The download directory must already exist.**
- **Everything is `async`.** Forgetting `await` gives you a coroutine object, not data.
