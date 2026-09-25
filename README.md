# graphplug

**Microsoft Graph for Python, one call at a time.** Send a mail, book a Teams meeting, upload a
file or look someone up in the directory without writing Graph's JSON by hand. Sign-in, retries,
throttling, paging and batching are handled for you.

```bash
pip install graphplug
```

Python 3.10+ · async · depends only on Microsoft's `azure-identity` and `msgraph-core`

---

## Contents

1. [Quick start](#1-quick-start)
2. [Signing in](#2-signing-in)
3. [Permissions (`Scopes`)](#3-permissions-scopes)
4. [Mail](#4-mail--graphmail)
5. [Calendar and meetings](#5-calendar-and-meetings--graphcalendar)
6. [Files (OneDrive)](#6-files-onedrive--graphfiles)
7. [Teams and chats](#7-teams-and-chats--graphteams)
8. [People and the directory](#8-people-and-the-directory--graphusers)
9. [Methods every resource has](#9-methods-every-resource-has)
10. [Any other Graph endpoint](#10-any-other-graph-endpoint)
11. [Paging](#11-paging)
12. [Batching](#12-batching)
13. [Downloads and uploads](#13-downloads-and-uploads)
14. [Errors](#14-errors)
15. [Logging](#15-logging)
16. [Client lifetime and concurrency](#16-client-lifetime-and-concurrency)

---

## 1. Quick start

Everything is `async`, so a script wraps its work in `async def main()` and runs it with
`asyncio.run`.

```python
import asyncio
from graphplug import GraphClient, Scopes

TENANT = "your-tenant-id"
CLIENT = "your-app-client-id"

async def main():
    # Prints a code; you sign in on any browser, then the script continues.
    graph = await GraphClient.device_code(TENANT, CLIENT, Scopes.MAIL_SEND)

    async with graph:
        await graph.mail.send(
            to="alice@example.com",
            subject="Quarterly report",
            body="<p>Attached.</p>", html=True,
            attachments=["report.pdf"],
        )

asyncio.run(main())
```

The client exposes five resources plus generic access to all of Graph:

| Attribute | What it covers |
|---|---|
| `graph.mail` | Send, reply, forward, read and tidy mail |
| `graph.calendar` | Book meetings (with Teams links), respond, find free times |
| `graph.files` | Upload, download, share and organise OneDrive files |
| `graph.teams` | Post to channels, reply in threads, send chat messages |
| `graph.users` | Find people, managers, reports, groups and photos |
| `graph.get/post/patch/delete`, `graph.paged`, `graph.batch` | Any other Graph endpoint |

---

## 2. Signing in

### Pick an access model first

| | **Application access** | **Delegated access** |
|---|---|---|
| Acts as | Your app itself | A signed-in person |
| Can reach | **The whole tenant** | Only what that person can already reach |
| `graph.mail`, `graph.calendar`, `graph.files`, `graph.teams` | No (there is no "me") | Yes |
| `graph.users` | Yes (pass `user` where a method takes one) | Yes |
| Needs a person | No | Yes, at sign-in |
| Entra app registration | Client secret, *application* permissions, admin consent | No secret, *delegated* permissions, "Allow public client flows" on |

`Mail.Read` as an application permission reads **every mailbox in the tenant**. As a delegated
permission it reads only the signed-in person's mail. Choose deliberately.

### Ways to build a client

| Constructor | Access | Sync or async | Use it when |
|---|---|---|---|
| `GraphClient.app_only(tenant_id, client_id, client_secret)` | Application | sync | A service or scheduled job with a client secret |
| `GraphClient.from_env()` | Application | sync | Same, reading `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` |
| `GraphClient.device_code(tenant_id, client_id, scopes)` | Delegated | `await` | A script or terminal; the person signs in on any device |
| `GraphClient.begin_device_code(...)` | Delegated | `await` | Same, but you show the code yourself (a UI, a chat message) |
| `GraphClient.interactive(tenant_id, client_id, scopes)` | Delegated | `await` | A desktop machine; opens the browser and catches the sign-in |
| `GraphClient.from_credential(credential)` | Either | sync | Managed identity, certificates, on-behalf-of, or any `azure-identity` credential |

Every constructor also accepts `max_concurrency` (default `12`), and the Entra ones accept
`authority_host` for national clouds.

**Application access, from environment variables**

```python
async with GraphClient.from_env() as graph:
    async for user in graph.paged("/users", select="id,displayName,mail"):
        print(user["displayName"], user["mail"])
```

**Application access, explicit**

```python
graph = GraphClient.app_only(TENANT, CLIENT, client_secret=SECRET)
```

**Delegated, device code**

```python
scopes = Scopes.combine(Scopes.MAIL_SEND, Scopes.USER_READ)
graph = await GraphClient.device_code(TENANT, CLIENT, scopes)
```

**Delegated, device code with your own prompt**

```python
pending = await GraphClient.begin_device_code(TENANT, CLIENT, Scopes.CALENDARS_READ_WRITE)
print(pending.message)            # or pending.user_code and pending.verification_uri
graph = await pending.complete()  # waits until the person has signed in
```

| `PendingSignIn` member | Meaning |
|---|---|
| `message` | Microsoft's ready-to-print instruction |
| `user_code`, `verification_uri` | The code and where to enter it |
| `expires_in` | Seconds before the code expires |
| `await complete()` | Wait for the person, return a `GraphClient` |
| `await cancel()` | Give up and release the sign-in |

**Delegated, browser on this machine**

```python
graph = await GraphClient.interactive(TENANT, CLIENT, Scopes.FILES_READ_WRITE,
                                      redirect_uri="http://localhost:8400")
```

The `redirect_uri` must match the app registration exactly: `http://localhost:8400` and
`http://localhost:8400/` are different to Entra.

**Any `azure-identity` credential**

```python
from azure.identity.aio import ManagedIdentityCredential

graph = GraphClient.from_credential(ManagedIdentityCredential())
```

Sync credentials work too and run on a worker thread. A credential you pass in stays yours:
closing the client does not close it.

---

## 3. Permissions (`Scopes`)

Delegated sign-ins need explicit scopes; there is no default. `Scopes` names them so you do not
have to remember Graph's spelling.

| Constant | Graph permission | Needed for |
|---|---|---|
| `Scopes.USER_READ` | `User.Read` | `users.me()` |
| `Scopes.USER_READ_ALL` | `User.Read.All` | Other people: `find`, `by_email`, `manager`, `reports`, `groups` |
| `Scopes.MAIL_READ` | `Mail.Read` | Reading mail |
| `Scopes.MAIL_READ_WRITE` | `Mail.ReadWrite` | Marking, moving, deleting mail |
| `Scopes.MAIL_SEND` | `Mail.Send` | `mail.send`, `reply`, `forward` |
| `Scopes.CALENDARS_READ` | `Calendars.Read` | Reading events |
| `Scopes.CALENDARS_READ_WRITE` | `Calendars.ReadWrite` | Booking, responding, cancelling |
| `Scopes.FILES_READ` | `Files.Read` | Reading your files |
| `Scopes.FILES_READ_WRITE` | `Files.ReadWrite` | Uploading, sharing, deleting your files |
| `Scopes.FILES_READ_WRITE_ALL` | `Files.ReadWrite.All` | Other people's drives and SharePoint libraries |
| `Scopes.TEAM_READ_BASIC` | `Team.ReadBasic.All` | Listing teams and channels |
| `Scopes.CHANNEL_MESSAGE_SEND` | `ChannelMessage.Send` | `teams.post`, `teams.reply` |
| `Scopes.CHANNEL_MESSAGE_READ` | `ChannelMessage.Read.All` | `teams.messages` (a protected API; see §7) |
| `Scopes.CHAT_READ_WRITE` | `Chat.ReadWrite` | `teams.chats`, `teams.send_chat` |
| `Scopes.EVERYTHING` | Everything the resources use except `Files.ReadWrite.All` and `ChannelMessage.Read.All` | A first run; narrow it afterwards |

Combine groups with `Scopes.combine(...)`, which keeps order and drops duplicates:

```python
scopes = Scopes.combine(Scopes.MAIL_SEND, Scopes.CALENDARS_READ_WRITE, Scopes.USER_READ)
```

Application access ignores these: it always uses the permissions an admin granted to the app.

---

## 4. Mail — `graph.mail`

Delegated access. Works on the signed-in person's mailbox.

| Method | What it does | Returns |
|---|---|---|
| `await send(to, subject, body="", ...)` | Send a message | `None` |
| `await send_many(messages)` | Send many messages, 20 per round-trip | list of per-message results |
| `compose(to, subject, body="", ...)` | Build the message JSON without sending | `dict` |
| `await reply(message_id, comment="", reply_all=False)` | Reply, or reply to all | `None` |
| `await forward(message_id, to, comment="")` | Forward to new recipients | `None` |
| `inbox(unread_only=False, since=None, search=None, top=50, select=...)` | Walk the inbox, newest first | async iterator of messages |
| `await mark_read(message_id, read=True)` | Mark read or unread | the updated message |
| `await move(message_id, folder)` | Move to a folder name (`"archive"`, `"deleteditems"`) or folder id | the moved message |
| `await delete_many(message_ids)` | Delete many messages in batches | list of per-message results |

**`send` parameters**

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `to` | `str` or list of `str` | required | Recipients |
| `subject` | `str` | required | Subject line |
| `body` | `str` | `""` | Text, or HTML when `html=True` |
| `cc`, `bcc`, `reply_to` | `str` or list | `None` | More recipients |
| `html` | `bool` | `False` | Treat `body` as HTML |
| `attachments` | list of file paths | `None` | Files to attach; about 3 MB in total at most |
| `save_to_sent` | `bool` | `True` | Keep a copy in Sent Items |

```python
await graph.mail.send(
    to=["alice@example.com", "bob@example.com"],
    cc="team@example.com",
    subject="Build finished",
    body="<b>All green.</b>", html=True,
    attachments=["results.csv"],
)

# Read unread mail from the last day, then mark it read
from datetime import datetime, timedelta, timezone
since = datetime.now(timezone.utc) - timedelta(days=1)
async for message in graph.mail.inbox(unread_only=True, since=since):
    print(message["subject"], "from", message["from"]["emailAddress"]["address"])
    await graph.mail.mark_read(message["id"])

# Search (Graph does not allow search together with filters or sorting)
async for message in graph.mail.inbox(search="invoice"):
    print(message["subject"])

# Reply and forward
await graph.mail.reply(message_id, "Thanks, on it.", reply_all=True)
await graph.mail.forward(message_id, to="carol@example.com", comment="FYI")

# Send many at once; each entry takes the same arguments as send()
results = await graph.mail.send_many([
    {"to": "a@example.com", "subject": "Hi A", "body": "..."},
    {"to": "b@example.com", "subject": "Hi B", "body": "..."},
])
failed = [r for r in results if r["status"] >= 400]
```

For attachments over about 3 MB, upload the file with `graph.files.upload` and send a
`share_link` instead.

---

## 5. Calendar and meetings — `graph.calendar`

Delegated access. Works on the signed-in person's calendar.

| Method | What it does | Returns |
|---|---|---|
| `await schedule(subject, start, end, ...)` | Create an event or Teams meeting | the created event |
| `await schedule_many(events)` | Create many events in batches | list of per-event results |
| `compose(subject, start, end, ...)` | Build the event JSON without creating it | `dict` |
| `upcoming(days=7, select=..., top=50)` | Events in the next `days`, soonest first, recurring ones expanded | async iterator of events |
| `await respond(event_id, response, comment="", send_response=True)` | `response` is `"accept"`, `"decline"` or `"tentativelyAccept"` | `None` |
| `await cancel(event_id, comment="")` | Cancel a meeting you organise and notify attendees | `None` |
| `await find_times(attendees, duration_minutes=30, within_days=5, minimum_attendance_percent=100)` | Ask Graph for slots that suit everyone | list of suggestions |
| `await free_busy(people, start, end, interval_minutes=30)` | Each person's availability over a window | list of schedules |

**`schedule` parameters**

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `subject` | `str` | required | Title |
| `start`, `end` | `datetime` | required | `end` must be after `start` |
| `attendees` | `str` or list | `None` | Required attendees |
| `optional_attendees` | `str` or list | `None` | Optional attendees |
| `online` | `bool` | `False` | Add a Teams meeting link |
| `location` | `str` | `None` | Room or place name |
| `body`, `html` | `str`, `bool` | `""`, `False` | Description |
| `timezone_name` | `str` | `"UTC"` | Zone for times without a timezone, e.g. `"GMT Standard Time"` |
| `reminder_minutes` | `int` | `None` | Reminder before start |
| `all_day` | `bool` | `False` | All-day event (use midnight-to-midnight times) |

```python
from datetime import datetime, timedelta, timezone

start = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
event = await graph.calendar.schedule(
    subject="Design review",
    start=start, end=start + timedelta(minutes=45),
    attendees=["alice@example.com", "bob@example.com"],
    online=True,
    reminder_minutes=10,
)
print(event["onlineMeeting"]["joinUrl"])

# Let Graph pick a time that suits everyone
suggestions = await graph.calendar.find_times(["alice@example.com", "bob@example.com"],
                                              duration_minutes=30)
best = suggestions[0]["meetingTimeSlot"]

# What is coming up
async for item in graph.calendar.upcoming(days=3):
    print(item["subject"], item["start"]["dateTime"])

# Answer an invitation
await graph.calendar.respond(event_id, "accept", comment="See you there")
```

Timezone-aware datetimes are sent in UTC; naive ones are read in `timezone_name`.

---

## 6. Files (OneDrive) — `graph.files`

Delegated access. Every method takes either a **drive path** (starts with `/`, e.g.
`"/reports/q3.xlsx"`) or an **item id**. Names with spaces, `#` or `?` are handled for you.

| Method | What it does | Returns |
|---|---|---|
| `await upload(source, to=None)` | Upload a local file; large files switch to a resumable upload on their own | the uploaded item |
| `await download(item, dest_path)` | Save a file to disk (the folder must exist) | `{"bytesWritten", "destPath", ...}` |
| `folder(path="/", select=..., top=200)` | List what is directly inside a folder | async iterator of items |
| `search(query, select=...)` | Search the whole drive by name and content | async iterator of items |
| `await metadata(item, select=...)` | One item's properties | `dict` |
| `await make_folder(path, conflict="fail")` | Create a folder; `conflict` is `"fail"`, `"rename"` or `"replace"` | the new folder |
| `await share_link(item, kind="view", scope="organization")` | Create a sharing link | the URL as `str` |
| `await remove(item)` | Move to the recycle bin | `None` |

**`share_link` options**

| Parameter | Values | Meaning |
|---|---|---|
| `kind` | `"view"`, `"edit"`, `"embed"` | What the link allows |
| `scope` | `"organization"`, `"users"`, `"anonymous"` | Who it works for; `"anonymous"` is often disabled by tenant policy |

```python
await graph.files.make_folder("/reports/2026", conflict="rename")
item = await graph.files.upload("q3.xlsx", to="/reports/2026/q3.xlsx")

url = await graph.files.share_link("/reports/2026/q3.xlsx", kind="edit")

async for entry in graph.files.folder("/reports/2026"):
    kind = "folder" if "folder" in entry else "file"
    print(kind, entry["name"], entry.get("size"))

async for hit in graph.files.search("budget"):
    print(hit["name"], hit["webUrl"])

await graph.files.download("/reports/2026/q3.xlsx", "downloads/q3.xlsx")
await graph.files.remove("/reports/2026/old.xlsx")
```

Omit `to` in `upload` and the file keeps its own name at the drive root.

---

## 7. Teams and chats — `graph.teams`

Delegated access in practice. Reading channel messages with *application* permissions is a
protected Graph API that Microsoft must approve first; posting as a signed-in person works
normally.

| Method | What it does | Returns |
|---|---|---|
| `mine(select=...)` | Teams the signed-in person belongs to | async iterator of teams |
| `channels(team_id, select=...)` | Channels in a team | async iterator of channels |
| `await channel_by_name(team_id, name)` | Find a channel by display name (case-insensitive) | the channel |
| `members(team_id)` | Who is in a team | async iterator of members |
| `await post(team_id, channel_id, message, html=False, subject=None, importance="normal")` | Post to a channel; `importance` is `"normal"`, `"high"` or `"urgent"` | the created message |
| `await reply(team_id, channel_id, message_id, message, html=False)` | Reply in a channel thread | the reply |
| `messages(team_id, channel_id, top=50, select=...)` | A channel's messages, newest first (replies not included) | async iterator of messages |
| `chats(select=...)` | The signed-in person's chats | async iterator of chats |
| `await send_chat(chat_id, message, html=False)` | Send into an existing chat | the created message |

```python
async for team in graph.teams.mine():
    print(team["id"], team["displayName"])

channel = await graph.teams.channel_by_name(team_id, "deploys")
posted = await graph.teams.post(team_id, channel["id"], "<b>v2.1 is live</b>",
                                html=True, subject="Release", importance="high")

# Replies attach to the thread's first message, so pass the id post() returned
await graph.teams.reply(team_id, channel["id"], posted["id"], "Rollback plan is in the wiki.")

async for chat in graph.teams.chats():
    if chat.get("topic") == "On-call":
        await graph.teams.send_chat(chat["id"], "Paging you now")
```

Starting a new chat is not wrapped yet; use `graph.post("/chats", body=...)`.

---

## 8. People and the directory — `graph.users`

Works under both access models. Where a method takes `user`, leave it out for the signed-in
person, or pass an id or user principal name. Under application access there is no signed-in
person, so pass `user`.

| Method | What it does | Returns |
|---|---|---|
| `await me(select=...)` | The signed-in person (delegated only) | `dict` |
| `find(query, top=25, select=...)` | Search display names and mail addresses | async iterator of users |
| `await by_email(address, select=...)` | Exactly one user by mail address; raises `itemNotFound` if none | `dict` |
| `await manager(user=None)` | Who this person reports to | `dict` |
| `reports(user=None, select=...)` | Direct reports | async iterator of users |
| `groups(user=None, select=...)` | Groups this person belongs to directly | async iterator of groups |
| `await photo(dest_path, user=None, size=None)` | Save the profile photo; `size` like `"96x96"` | download summary |

```python
me = await graph.users.me()
print(me["displayName"], me["jobTitle"])

async for person in graph.users.find("smith"):
    print(person["displayName"], person["mail"])

alice = await graph.users.by_email("alice@example.com")
boss = await graph.users.manager(alice["id"])

async for report in graph.users.reports():
    print("reports to me:", report["displayName"])

await graph.users.photo("me.jpg", size="96x96")
```

`find` adds the `ConsistencyLevel: eventual` header Graph needs for directory search, on every
page.

---

## 9. Methods every resource has

`mail`, `calendar`, `files`, `teams` and `users` all share these, working on the resource's own
collection (`/me/messages`, `/me/events`, `/me/drive/items`, `/teams`, `/users`).

| Method | What it does | Returns |
|---|---|---|
| `list(**odata)` | Every item, page by page | async iterator |
| `await get(item_id, **odata)` | One item | `dict` |
| `await create(body)` | Create an item from raw Graph JSON | the created item |
| `await update(item_id, body)` | Change fields | the updated item |
| `await delete(item_id)` | Delete an item | `None` |
| `await get_many(item_ids, select=None)` | Fetch many items in batches of 20, in the order given | list of results |

```python
message = await graph.mail.get(message_id, select="subject,body")
await graph.calendar.update(event_id, {"subject": "Moved: Design review"})

people = await graph.users.get_many(["id-1", "id-2", "id-3"], select="displayName,mail")
for result in people:
    if result["status"] == 200:
        print(result["body"]["displayName"])
```

---

## 10. Any other Graph endpoint

Every Graph endpoint is reachable without a dedicated wrapper.

| Method | What it does | Returns |
|---|---|---|
| `await graph.get(path, **odata)` | `GET` | the response body |
| `await graph.post(path, body=None, **odata)` | `POST` | the response body |
| `await graph.patch(path, body=None, **odata)` | `PATCH` | the response body |
| `await graph.delete(path, **odata)` | `DELETE` | the response body (usually `None`) |
| `await graph.request(method, path, version=None, body=None, headers=None, **odata)` | Any method, with the full response | `{"status", "headers", "body", "nextLink"}` |

**OData options** are plain keyword arguments; the `$` is added for you.

| Keyword | Sent as | Example |
|---|---|---|
| `select` | `$select` | `select="id,displayName"` |
| `filter` | `$filter` | `filter="startswith(displayName,'A')"` |
| `orderby` | `$orderby` | `orderby="displayName"` |
| `top` | `$top` | `top=50` |
| `skip` | `$skip` | `skip=100` |
| `expand` | `$expand` | `expand="manager"` |
| `search` | `$search` | `search='"displayName:smith"'` |
| `count` | `$count` | `count=True` |

Any other keyword is sent as a plain query parameter.

**Other options**

| Option | Meaning |
|---|---|
| `version="beta"` | Use Graph's beta endpoint instead of `v1.0` |
| `headers={...}` | Extra request headers (an `Authorization` header is refused; the client owns sign-in) |
| A full `https://` URL as `path` | Used as-is, e.g. a `nextLink` |

```python
org = await graph.get("/organization", select="displayName,verifiedDomains")

group = await graph.post("/groups", body={
    "displayName": "Project X", "mailNickname": "projectx",
    "mailEnabled": False, "securityEnabled": True,
})

profile = await graph.get("/me/profile", version="beta")

envelope = await graph.request("GET", "/users", top=5, count=True,
                               headers={"ConsistencyLevel": "eventual"})
print(envelope["status"], envelope["body"]["@odata.count"], envelope.get("nextLink"))
```

Response headers are limited to a safe list: `request-id`, `client-request-id`, `Date`,
`Retry-After`, `Content-Type`, `Location` and `ETag`.

---

## 11. Paging

`graph.paged(path, **odata)` follows `@odata.nextLink` for you and yields one item at a time.
Nothing is held in memory beyond the current page, and you can stop early with `break`.

| Parameter | Meaning |
|---|---|
| `path` | Collection path, e.g. `"/users"` |
| `version` | `"v1.0"` (default) or `"beta"` |
| `headers` | Sent with every page, e.g. `{"ConsistencyLevel": "eventual"}` |
| `**odata` | `select`, `filter`, `top` and the rest (see §10) |

```python
count = 0
async for user in graph.paged("/users", select="id,mail", filter="accountEnabled eq true"):
    count += 1
    if count == 1000:
        break
```

---

## 12. Batching

`graph.batch(requests)` sends many requests with few round-trips. It splits them into groups of
20 (Graph's limit), sends the groups at the same time, and returns results **in the order you
gave**.

| Request form | Example |
|---|---|
| A tuple | `("GET", "/users/alice@example.com")` |
| A dict | `{"method": "PATCH", "url": "/me/messages/ID", "body": {"isRead": True}, "headers": {"Content-Type": "application/json"}}` |

Each result has `id`, `status`, `body` and sometimes `headers`. Failures are reported in place,
not raised, so one bad request never loses the others. Only a batch where every group failed
raises a `GraphError`.

```python
results = await graph.batch([
    ("GET", "/me"),
    ("GET", "/me/manager"),
    ("GET", "/me/drive/root/children?$top=5"),
])
for result in results:
    print(result["status"], result["body"])
```

Batch URLs are relative and have no version prefix. Pass `version="beta"` to `batch` to switch
all of them.

---

## 13. Downloads and uploads

For files outside `graph.files`, the generic transfer methods take any Graph path.

| Method | What it does | Returns |
|---|---|---|
| `await graph.download(path, dest_path, version=None)` | Stream a response to disk. The folder must exist; a failed download leaves no partial file | `{"status", "headers", "bytesWritten", "destPath"}` |
| `await graph.upload(path, source_path, version=None)` | Send a file. Under 4 MiB: one request. 4 MiB and over: a resumable upload in 10 MiB chunks | the Graph response, plus `"bytesSent"` |

```python
await graph.download("/me/photo/$value", "me.jpg")
await graph.upload("/sites/SITE_ID/drive/root:/Shared/plan.pdf:/content", "plan.pdf")
```

---

## 14. Errors

Every failure raises one exception type, `GraphError`, whether it came from Graph, the network
or sign-in.

| Attribute | Meaning |
|---|---|
| `status` | HTTP status, or `0` when there was no response (network, sign-in) |
| `code` | Graph's error code (`"itemNotFound"`) or a package code (below) |
| `message` | Human-readable detail |
| `request_id` | Quote this to Microsoft support |
| `retry_after` | Seconds to wait, when Graph said so |
| `inner` | Graph's own inner error, unchanged |

```python
from graphplug import GraphError

try:
    await graph.users.by_email("nobody@example.com")
except GraphError as e:
    if e.code == "itemNotFound":
        print("no such user")
    elif e.status == 429:
        print(f"throttled; retry in {e.retry_after}s")
    else:
        print(f"[{e.status} {e.code}] {e.message} (request {e.request_id})")
```

**Package codes** (`status` is `0`)

| `code` | Cause |
|---|---|
| `authenticationFailed` | Wrong secret, tenant or client id |
| `consentRequired` | A requested permission has not been consented |
| `interactionRequired` | The signed-in session expired; sign in again |
| `signInTimeout` | Nobody finished signing in within the window |
| `signInDeclined` | The person refused or cancelled |
| `stateMismatch` | The browser redirect did not match the sign-in that started it |
| `transportError` | DNS, TLS or connection failure |
| `timeout` | The request timed out |
| `invalidRequest` | A bad argument, such as missing `scopes` or a missing file |
| `invalidHandle` | The client was already closed |

Throttling (`429`) and temporary server errors are retried automatically, honouring
`Retry-After`, before an error ever reaches you. Every code and its fix is in the
[troubleshooting guide](https://github.com/VikramBalaji1/graphPlug/blob/main/docs/troubleshooting.md).

---

## 15. Logging

Off by default. Turn it on with an environment variable:

| `GRAPHPLUG_LOG_LEVEL` | Logs |
|---|---|
| unset / anything else | Nothing |
| `error` | Failed requests only |
| `info` | Every request |

One JSON line per event, on stderr:

```
{"level":"info","event":"request","method":"GET","url":"https://graph.microsoft.com/v1.0/users","status":200,"ms":214,"requestId":"a1b2c3d4","errorCode":null}
```

URLs are logged without their query string, and tokens, secrets, headers and bodies are never
logged.

---

## 16. Client lifetime and concurrency

| Topic | Behaviour |
|---|---|
| Closing | Use `async with graph:` or `await graph.aclose()`. Closing twice is harmless; using a closed client raises `invalidHandle` |
| Concurrency | Safe to use from many tasks at once in one event loop. At most `max_concurrency` (default `12`) requests are in flight; the rest wait |
| Event loops | Create and use a client inside the same event loop |
| Tokens | Fetched, cached in memory and refreshed automatically; nothing is written to disk |
| Cancellation | Cancel the calling task, or wrap a call in `asyncio.wait_for(..., timeout=...)` |

```python
import asyncio

async with GraphClient.from_env(max_concurrency=8) as graph:
    ids = ["alice@example.com", "bob@example.com", "carol@example.com"]
    people = await asyncio.gather(*(graph.users.get(i) for i in ids))
```

---

## More

- [USAGE.md](https://github.com/VikramBalaji1/graphPlug/blob/main/USAGE.md): a longer walkthrough
- [ARCHITECTURE.md](https://github.com/VikramBalaji1/graphPlug/blob/main/ARCHITECTURE.md): why it is built the way it is
- [Troubleshooting](https://github.com/VikramBalaji1/graphPlug/blob/main/docs/troubleshooting.md): every error code and its fix
- [Samples](https://github.com/VikramBalaji1/graphPlug/tree/main/samples): runnable scripts

## Development

```bash
pip install -e .
python -m unittest discover -s tests
python -m build
```

### Releasing

Pushing a version tag publishes to PyPI through GitHub Actions (trusted publishing, no token).
Ordinary commits only run the tests.

1. Set `__version__` in `graphplug/__init__.py` (for example `"0.2.2"`) and commit.
2. Tag that commit and push the tag:

   ```bash
   git tag v0.2.2
   git push origin main v0.2.2
   ```

The workflow tests, builds, checks, and uploads. It refuses to publish if the tag and
`__version__` differ, because PyPI accepts each version only once.

## Licence

[MIT](https://github.com/VikramBalaji1/graphPlug/blob/main/LICENSE)
