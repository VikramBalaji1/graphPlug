# Technical Architecture

A plug-and-play Python package for Microsoft Entra ID authentication and Microsoft Graph, async
throughout, built on `azure-identity` and `msgraph-core`.

Supports both Entra ID access models — **application-level access** (app permissions, no user) and
**delegated access** (on behalf of a signed-in person), and adds a **resource layer** so that the
common jobs — send a mail, book a Teams meeting — are one call rather than twenty lines of nested
JSON.

> **History.** Until `33f4076` this package was a C# core compiled to a native library and reached
> from Python through a C ABI. That implementation has been deleted. Most of the rules below — the
> header allowlist, the error taxonomy, the upload thresholds, the batch chunking, the two access
> models — were worked out there and survived the rewrite unchanged, which is the best evidence
> available that they were about Graph rather than about C#.

---

## 1. Purpose and scope

Microsoft Graph and Azure Identity are already simple. This package exists to make them *plug and
play*: import one class, hand it credentials, and call Graph.

```python
import asyncio
from graphplug import GraphClient, Scopes

async def main():
    # Application-level access — app permissions, no user
    async with GraphClient.app_only(tenant_id=..., client_id=..., client_secret=...) as graph:
        async for user in graph.paged("/users", select="id,displayName,mail"):
            print(user["mail"])

    # Delegated access — a real person signs in, their permissions apply
    graph = await GraphClient.device_code(tenant_id=..., client_id=..., scopes=Scopes.MAIL_SEND)
    async with graph:
        await graph.mail.send(to="alice@contoso.com", subject="Hi", body="Hello")

asyncio.run(main())
```

**In scope**

- **Application-level access:** client-credentials (client secret).
- **Delegated access:** device code, and authorization code with PKCE.
- Generic authenticated requests to any Graph `v1.0` or `beta` endpoint.
- OData parameters, custom headers, request bodies.
- Pagination, batching, file download, file upload including large-file sessions.
- A resource layer over mail, calendar, files, teams and users.
- Consistent, structured error information.
- Retry and throttling through Microsoft's supported middleware.
- A pure-Python `py3-none-any` wheel.

**Out of scope** — see §9 for each omission and the trigger that should bring it in.

---

## 2. Decisions and rationale

| # | Decision | Rationale | Rejected alternative |
|---|---|---|---|
| D1 | **Pure Python over `azure-identity` + `msgraph-core`** | Installs anywhere, no compiler, no container, no platform matrix. Two direct dependencies, both Microsoft's. | *A compiled core behind a C ABI* — what this package used to be. It bought nothing once the .NET consumer was dropped, and cost a `linux-x64`-only wheel and a Docker-only build. |
| D2 | **`msgraph-core`, not `msgraph-sdk`** | Supplies the supported `GraphClientFactory` middleware — retry, `Retry-After`, redirect, telemetry. Resolves in about four seconds. | The full `msgraph-sdk` — its dependency tree does not resolve in practice: `pip` and `uv` both hang indefinitely. Measured, not assumed. |
| D3 | **Async-native** | Safe concurrency by construction — single-threaded, no data races. `msgraph-core`'s middleware is `async def` only, so a sync surface would mean writing the 429 loop by hand. | A sync surface over `requests` — friendlier to a beginner, but `requests.Session` is not officially thread-safe and sync `httpx` retries connection errors only, not status codes. |
| D4 | **Concurrency inside the library, never in the caller** | The ergonomics cost of async is paid once, here. Callers write `await graph.mail.send_many(...)`, never `asyncio.gather`. | Exposing raw coroutines and letting callers fan out — which is how you get throttled. |
| D5 | **Both access models** | The package must serve a daemon with application permissions *and* a tool acting for a person. Two Entra app registrations, two token stories (§4). | App-only alone — simpler, but leaves every "act as the signed-in user" scenario unreachable. |
| D6 | **Delegated via device code and auth-code + PKCE** | Device code fits headless scripts and containers; auth-code + PKCE gives a better desktop experience. Python is a genuine public client, so PKCE needs no workaround. | *ROPC* — breaks under MFA and Conditional Access, and stores user passwords. |
| D7 | **In-memory token cache only** | No credential material at rest, no keyring dependency, identical behaviour inside a container. | A persistent cache — better repeat-run UX, at the cost of a refresh token on disk. |
| D8 | **Paging, batching and chunked upload are ours** | Kiota's `PageIterator`, `BatchRequestBuilder` and `LargeFileUploadTask` each require a `RequestAdapter` and `Parsable` models this package deliberately never builds. | Adopting Kiota's abstractions — which would mean adopting the generated type system, i.e. `msgraph-sdk`, i.e. D2. |
| D9 | **Plain dicts out, friendly arguments in** | `graph.mail.send(to=..., subject=...)` on the way in; Graph's own JSON on the way out. Nothing to learn on the return path, and every field Graph adds tomorrow is already there. | Typed response models — a second schema to maintain against a service that changes weekly. |
| D10 | **Polymorphism at the variation points** | Multiple sign-in flows, two upload strategies and a growing set of resources are real, enumerable variation. A base class plus thin subclasses makes each addition a new class rather than an edit. | A procedural core with `if` chains — smaller today, and the thing every new flow has to cut into. |
| D11 | **`httpx.MockTransport` as the only test seam** | The mock sits *under* the real middleware, so tests exercise the same retry and redirect path production does. | Patching the package's own functions — which tests the test double, not the pipeline. |

---

## 3. Design principles

CLAUDE.md asks for the smallest package that works and warns against abstractions for single-use
code. The project also calls for object-oriented design and reuse. One rule reconciles them:

> **Abstract at real variation points. Stay concrete everywhere else.**

A variation point is real when it is *enumerable and populated*: two or more implementations exist
today, or a named third is scheduled. Speculative extensibility is not a variation point.

**Where polymorphism earns its place**

| Abstraction | Implementations today | Pattern |
|---|---|---|
| `GraphResource` | `Mail`, `Calendar`, `Files`, `Teams`, `Users` | Template method |
| Upload strategy (`_upload_simple` / `_upload_chunked`, chosen by size) | simple `PUT`, chunked session | Strategy |
| Credential | `ClientSecretCredential`, `_SyncCredentialAdapter`, `_MsalCredential` | Duck-typed on `get_token` |
| Sign-in flow | device code, authorization code | Two-phase begin/complete |

`GraphResource` is the load-bearing one. Every Graph collection supports list/get/create/update/
delete over a different path, so adding a resource is one subclass that sets `path` and `scopes`
and adds only what is specific to it. Nothing else in the package moves.

**Where it does not, and why**

- **No credential interface.** `azure-identity` already defines one; anything with an async
  `get_token` is accepted. Declaring a parallel `Protocol` would add a name, not a seam.
- **No client/session split.** `GraphClient` owns one `Transport`, and `Transport` owns the
  pipeline. An earlier draft had a third object between them that held one field.
- **No exception hierarchy.** One `GraphError` carrying `status` and `code` (§8).
- **No DI container.** Six modules and an explicit composition root.
- **No abstract base with one subclass, anywhere.** If a hierarchy collapses to one
  implementation, it collapses back to a class.

**Conventions:** module-private everything except the four public names; `__all__` on every module;
constructor injection; `async with` on anything owning a connection or a credential; guard clauses
over nesting; names from the Graph and Entra domain rather than from patterns.

---

## 4. Component map

```text
┌─────────────────────────────── your script ───────────────────────────────┐
│                                                                           │
│  __init__.py      GraphClient · GraphError · PendingSignIn · Scopes       │
│                   app_only() · from_env() · device_code() · interactive() │
│                   get/post/patch/delete · paged · batch · download/upload │
│        │                                                                  │
│        ├── _resources/base.py   GraphResource: list · get · create ·      │
│        │        │               update · delete · get_many                │
│        │        ├── mail.py     graph.mail     send · reply · inbox · …   │
│        │        ├── calendar.py graph.calendar schedule · upcoming · …    │
│        │        ├── files.py    graph.files    upload · share_link · …    │
│        │        ├── teams.py    graph.teams    post · channels · …        │
│        │        └── users.py    graph.users    find · manager · photo …   │
│        │                                                                  │
│        ├── _operations.py  paging · batching · download · upload          │
│        ├── _request.py     URL building · OData · header allowlist        │
│        ├── _errors.py      GraphError · the code taxonomy                 │
│        ├── _scopes.py      named permissions                              │
│        └── _log.py         opt-in JSON lines on stderr                    │
│                     │                                                     │
│        ┌────────────▼──────────────────────────────────────────────────┐  │
│        │ _http.py  Transport: bearer attachment, allowed-host guard,    │ │
│        │           the concurrency semaphore                            │ │
│        ├────────────────────────────────────────────────────────────────┤ │
│        │ msgraph-core   redirect · retry · Retry-After · telemetry       │ │
│        │ azure-identity token acquisition, refresh, in-memory cache      │ │
│        │ httpx          the connection pool                              │ │
│        └────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────┘
                                    │  HTTPS
                                    ▼
                         graph.microsoft.com/{v1.0,beta}
```

### The one undocumented contract

`msgraph-core`'s transport runs its middleware only when the request carries an `options`
attribute:

```python
if self.pipeline and hasattr(request, 'options'):
```

That attribute is normally set by Kiota's `RequestAdapter`, which this package does not use.
Without it the whole pipeline — retry, `Retry-After`, redirect, telemetry — is silently skipped and
requests go straight to the socket. `_http.py` sets it, and `test_transport.py` fails loudly if the
pipeline stops engaging. This was found because a retry test recorded one attempt where two were
expected; nothing else would have shown it.

---

## 5. Authentication

### 5.1 Two access models

Entra offers two fundamentally different ways to reach Graph. They are normally **two separate app
registrations**, because their permission sets, consent models and threat profiles differ.

| | **Application-level access** | **Delegated access** |
|---|---|---|
| Acting as | The application itself | A signed-in person |
| Entra permission type | Application permissions | Delegated permissions |
| Consent | Admin consent, once, tenant-wide | User consent, or admin tenant-wide |
| Effective rights | Exactly the granted permissions, **across the whole tenant** | Intersection of the granted scopes **and** what that person can already do |
| `/me` works | No — there is no user | Yes |
| `graph.mail` / `graph.calendar` | No | Yes |
| Scope requested | `https://graph.microsoft.com/.default` | Explicit: `User.Read`, `Mail.Send`, … |
| Human needed | No | Yes, at first sign-in |
| Client type | Confidential (holds a secret) | Public (holds no secret) |
| Constructor | `app_only`, `from_env` | `device_code`, `interactive` |

The distinction that matters most in practice: **application permissions are not scoped to a user,
so they are tenant-wide.** `Mail.Read` as an application permission reads every mailbox in the
tenant. The same name as a delegated permission reads only the signed-in person's mail. Choosing
the wrong model is how a script ends up with far more reach than intended.

### 5.2 Entra app registration

The package cannot configure Entra; these are the settings it expects.

**Application-access app**

1. Register the app; note the *Directory (tenant) ID* and *Application (client) ID*.
2. **Certificates & secrets** → new client secret; record the value at creation.
3. **API permissions** → Microsoft Graph → **Application permissions** → add what is needed.
4. **Grant admin consent.** Application permissions are inert without it.
5. No redirect URI, no public client flows.

**Delegated-access app**

1. Register the app; note the tenant and client IDs. **No client secret** — it is a public client.
2. **API permissions** → Microsoft Graph → **Delegated permissions** → add the scopes needed.
3. **Authentication** → *Allow public client flows* → **Yes**. Device code fails without this.
4. **Authentication** → add a **Mobile and desktop applications** redirect URI matching what
   `interactive()` will use, e.g. `http://localhost:8400`. Not needed for device code alone.
5. Admin consent only if a requested scope requires it.

### 5.3 Scopes

App-only uses `.default`, which resolves to exactly the application permissions an administrator
has consented to. Scope minimisation happens where it belongs — in the app registration.

Delegated flows require **explicit scopes**, and the package refuses to default them. There is no
safe default: `.default` on a delegated flow silently requests every scope ever consented for that
client, which is the opposite of least privilege. Omitting `scopes` is an `invalidRequest` error
naming the field.

`Scopes` (in `_scopes.py`) exists so a caller need not know that sending mail requires `Mail.Send`.
Each resource also declares its own `scopes`, so the names are available where the failure happens.

### 5.4 Two-phase sign-in

A delegated flow must pause for a human, and the code must reach the caller *before* the wait
begins. Hence two phases:

```python
pending = await GraphClient.begin_device_code(tenant, client, scopes)
print(pending.message)          # "open …/devicelogin and enter FJKLMNPQ"
graph = await pending.complete()  # blocks until the person finishes
```

`device_code()` and `interactive()` are the one-call convenience over the same machinery.

Two failure modes shape the implementation:

- **A sign-in that fails before a code is ever issued.** A bad tenant never reaches the prompt
  callback, so waiting on the code alone would hang until the caller's timeout. `begin()` waits on
  the code *and* the sign-in task, whichever finishes first, and re-raises the real cause.
- **`azure.identity.aio` has no `DeviceCodeCredential`** — only the synchronous one exists. It runs
  under `asyncio.to_thread`, and `_SyncCredentialAdapter` presents it as async.

For the browser flow, `azure-identity`'s `AuthorizationCodeCredential` has no `code_verifier`
parameter, so PKCE goes through `msal.PublicClientApplication` instead. The redirect listener binds
to loopback only, serves exactly one request, and validates `state` internally — the CSRF check
cannot be skipped by a caller who forgets it.

> **Trap.** The redirect URI must reach Entra character for character. Normalising it — appending a
> trailing slash, say — produces AADSTS50011 at sign-in, far from the cause.

### 5.5 Token lifetime and credential material

Token acquisition, caching, expiry and refresh are entirely `azure-identity`'s. This package writes
no token-handling code: no manual token endpoint calls, no expiry arithmetic, no refresh timer.
Every line not written here is a line that cannot mishandle a token.

Caching is in-memory for the life of the client. App-only re-acquires from the secret as needed;
delegated signs in once per `GraphClient` and refreshes silently while it lives.

- Secrets are read from the environment and live only inside the credential.
- Never logged, never returned, never written to disk.
- **No method returns an access or refresh token.** A raw-token escape hatch would be a deliberate,
  separately reviewed addition.
- **The PKCE verifier never leaves the process.** It is credential material for the duration of the
  exchange; keeping it internal means nothing can leak it by logging a return value.
- **The bearer token is attached only for `graph.microsoft.com`.** Pre-authenticated download URLs
  still work; they simply travel unauthenticated.
- Response header allowlisting (§6.2) is the mechanical guarantee on the return path.

---

## 6. Request handling

### 6.1 URL construction

```text
absolute URL?  ──yes──►  use verbatim, ignore version and query
      │no
      ▼
https://graph.microsoft.com/{version}{path}?{urlencoded query}
```

Absolute-URL passthrough is what makes pagination, pre-authenticated download URLs and
`Location`-header follow-ups work without special cases.

> **Platform trap, inherited.** "Is this absolute?" must be decided on the `://` scheme separator,
> not on a URL parser's `scheme` field. The C# equivalent used `Uri.TryCreate(path,
> UriKind.Absolute, …)`, which returns `true` for `/users` on Unix — parsing it as `file:///users`
> — and `false` on Windows. It rejected every relative Graph path on Linux while the suite stayed
> green on the development machine. The scheme separator appears in every URL and in no Graph path.

OData parameters are passed as keyword arguments (`select=`, `filter=`, `top=`, …) and mapped to
their `$`-prefixed names. Python has no escaping problem here, so the mapping is a lookup.

### 6.2 Headers

Response headers reach the caller through an **allowlist**, never a denylist:

```text
request-id · client-request-id · Date · Retry-After · Content-Type · Location · ETag
```

A denylist fails open: whatever header Microsoft adds tomorrow would be returned by default. An
allowlist fails closed, so `Authorization` and `WWW-Authenticate` cannot come back even by
accident.

Request headers are forwarded as supplied except `Authorization`, which is **rejected**. The
package owns authentication; accepting a caller-supplied bearer token would silently bypass the
credential the client was built with.

### 6.3 Pagination

`paged()` is an async generator over `@odata.nextLink`. Nothing buffers the whole collection, and
an abandoned generator leaks nothing. `@odata.count` and `@odata.deltaLink` are left in the body
untouched, so `$count` and delta queries work with no extra support.

### 6.4 Batching

Batching, not asyncio, is where the speed comes from:

| | 500 user lookups |
|---|---|
| One at a time | 500 round-trips |
| `graph.batch(...)` | **25 round-trips** |
| …chunks dispatched concurrently | **~5 round-trip times** |

Two behaviours belong to the package:

1. **Chunking at 20.** Graph rejects larger batches. Chunks are dispatched concurrently under the
   semaphore, which is the second order-of-magnitude.
2. **Re-ordering by `id`.** Graph does not guarantee response order within a batch; input order is
   restored so results align positionally with what was sent.

Per-request failures inside a batch are **not** raised: one failing sub-request must not discard
nineteen successful ones. The caller gets a list with each sub-response's own status.

The same holds one level up. If a whole chunk fails -- a 429 after the retries run out, say --
its requests come back in place with that chunk's status, and the chunks that ran are kept. With
`send_many`, those mails have already gone; raising would hide that, and a caller retrying on the
exception would send them twice. Only a batch in which every chunk failed raises.

> **Known ceiling.** `dependsOn` chains are passed through but not validated across a chunk
> boundary — a dependency spanning two chunks will fail at Graph. Worth fixing only if someone
> actually uses ordered batches.

### 6.5 Downloads and uploads

Downloads stream to disk rather than into memory, so file size is bounded by disk. The package
writes to a temporary sibling and renames on success, so a failed download cannot leave a truncated
file at the destination. It does not create directories.

Upload strategy is selected by size — the two paths share nothing but their input:

| Size | Mechanism |
|---|---|
| < 4 MiB | Single `PUT` of the file |
| ≥ 4 MiB | `createUploadSession`, then sequential `PUT`s with `Content-Range` |

Chunk size is 10 MiB and **must be a multiple of 320 KiB**, which Graph requires for upload
sessions. Failed chunks resume from the session's `nextExpectedRanges`, which makes a large upload
resumable within a single call.

### 6.6 Concurrency and throttling

A bounded `asyncio.Semaphore` gates every request, defaulting to **12** and overridable per client.
The ceiling is the service's, not Python's: Graph throttles per application and per tenant, and
mailbox operations allow only a handful of concurrent requests per mailbox. Going wider earns 429s,
not throughput.

Throttling itself remains Microsoft's middleware honouring `Retry-After`. The semaphore reduces how
often that fires rather than replacing it.

One `httpx.AsyncClient` per `GraphClient`, so the connection pool and the token cache are shared.

---

## 7. The resource layer

This is where the plug-and-play goal lands. Each resource exists to absorb one piece of Graph that
is not guessable from the call site:

| Resource | The thing it absorbs |
|---|---|
| `Mail` | The `sendMail` payload: recipients as objects inside objects, a body with a content type, attachments base64-encoded behind an `@odata.type` discriminator |
| `Calendar` | `isOnlineMeeting` paired with `onlineMeetingProvider` for a Teams link, attendees carrying a `type`, and `dateTime`/`timeZone` pairs rather than ISO strings |
| `Files` | Drive addressing. `/me/drive/root:/reports/q3.xlsx:` by path versus `/me/drive/items/{id}` by id — including the *closing* colon, whose absence gives a 400 that mentions no colons |
| `Teams` | A two-level lookup before a message can be posted, and the `chatMessage` body shape shared by channel posts, replies and chats |
| `Users` | The `/me` versus `/users/{id}` fork on every directory call, and `ConsistencyLevel: eventual`, without which Graph refuses `$search`, `$count` and `endswith` with a bare 400 |

`Files` resolves path-versus-id on one rule — a leading slash means a path — so a caller never
writes a colon. `Users` defaults every `user` argument to the signed-in person.

```python
await graph.mail.send(to=..., subject=..., body=..., html=True, attachments=[...])
event = await graph.calendar.schedule(subject=..., start=..., end=..., online=True)
await graph.files.upload("q3.xlsx", to="/reports/2026/q3.xlsx")
await graph.teams.post(team_id, channel_id, "Report is up")
boss = await graph.users.manager()
```

`GraphResource` supplies `list`, `get`, `create`, `update`, `delete` and `get_many`; a subclass sets
`path` and `scopes` and adds what is specific to it. `get_many`, `send_many` and `schedule_many`
route through §6.4, so the fast path is the default rather than something a caller has to discover.

Resources are attached to the client as attributes (`graph.mail`, `graph.files`, …). Most are
delegated-access features, because they hang off `/me`, which does not exist under
application-level access. `Users` is the exception: it works under both, and its `user` arguments
stop being optional when there is no signed-in person.

> **A limit worth knowing rather than discovering.** Reading Teams channel messages with
> *application* permissions is one of Graph's protected APIs — Microsoft must approve the app
> before the call returns anything but 403, whatever consent the tenant has granted. Posting as a
> signed-in person is unaffected. Nothing here can work around it, so `Teams` says so in its
> docstring.

**Adding a resource** is one file: subclass `GraphResource`, set the path and scopes, add the
domain-specific methods, and attach it in `GraphClient.__init__`. Add one when there is a caller for
it, not before.

---

## 8. Error model

One exception type carrying data, not a hierarchy. Callers branch on `status` and `code`, which is
more precise than any class tree and does not require importing eight names to write an `except`.

```python
class GraphError(Exception):
    status: int             # HTTP status, or 0 when there was no response at all
    code: str               # "itemNotFound", or a package code from the table below
    message: str
    request_id: str | None  # quote this to Microsoft support
    retry_after: int | None
    inner: dict | None      # Graph's own inner error, verbatim
```

Graph's `innerError` is preserved untouched — it is frequently the only actionable part of a
failure.

Failures with no HTTP response use `status: 0` and a package-defined code:

| `code` | Cause |
|---|---|
| `authenticationFailed` | Bad secret, wrong tenant, expired credential |
| `consentRequired` | AADSTS65001 — a requested scope has no consent grant |
| `interactionRequired` | The signed-in session can no longer be refreshed |
| `signInTimeout` | AADSTS70016 — the person did not finish within the window |
| `signInDeclined` | AADSTS65004, or the person cancelled |
| `stateMismatch` | The redirect `state` did not match — a possible CSRF attempt |
| `transportError` | DNS, TLS, connection reset |
| `timeout` | The request timed out |
| `invalidRequest` | A malformed call, or a delegated flow missing `scopes` |
| `invalidHandle` | The client has already been closed |
| `internalError` | A bug in this package |

> **Trap, and the reason `flatten()` exists.** `azure-identity` puts the AADSTS detail on an
> **inner** exception; the outer message is a bare prefix like
> `"DeviceCodeCredential authentication failed: "`. Reading only the top message collapsed every
> delegated failure into `authenticationFailed`. Codes are matched on AADSTS numbers and OAuth
> error strings across the whole chain, never on prose — prose is localised and reworded.
>
> The same collapse reappeared on the browser flow and went unnoticed until that path was first
> tested. MSAL reports a refusal as a *string*, not an exception, and the classifier was being
> handed `GraphError(0, "authenticationFailed", detail)` to inspect — whereupon it returned that
> placeholder code, because its first branch trusts a `GraphError`'s own code. Matching is now
> `code_for_text`, which takes the string, and `code_for_exception` is a thin wrapper over it.

### Logging

Off unless asked: `GRAPHPLUG_LOG_LEVEL=info` or `=error`, one JSON object per line on stderr. URLs are
logged **without their query string**, because an OData `$filter` routinely carries email addresses.
Headers, bodies and credential material are never logged.

---

## 9. Deliberate omissions

Each is a decision, not an oversight. Each has a trigger.

| Omitted | Why | Add when |
|---|---|---|
| Persistent token cache | No credential material at rest, identical in a container (D7) | Repeated CLI runs make re-prompting tiresome enough to accept a refresh token on disk |
| Certificate / managed-identity / OBO *constructors* | `azure-identity` ships all three, and `from_credential` takes any of them, so a named constructor each would add surface without adding capability | A flow needs setup this package could do better than the caller passing a credential |
| ROPC (username/password) | Breaks under MFA and Conditional Access; stores user passwords | Effectively never; device code covers the headless case properly |
| Typed response models | Generic JSON reaches every `v1.0` and `beta` endpoint on day one (D9) | Never — anyone wanting typed builders should take `msgraph-sdk` if it ever resolves |
| A sync surface | Async is the safe-concurrency choice (D3), and `asyncio.run` is one line | Enough callers are in sync codebases to justify a generated sync mirror |
| More resources (Contacts, ToDo, Planner, SharePoint sites) | `GraphResource` is shaped for them; nothing is guessed in advance | There is a caller for one |
| Starting a new Teams chat, and @-mentions | Posting into an existing chat or channel is the common job; `POST /chats` and mention payloads are a step past it | Someone needs to open a conversation rather than continue one |
| Response caching | Graph's `ETag`/`If-None-Match` is passthrough already | A measured hot path re-fetches unchanged data |
| Dependency-aware batch partitioning | Sequential `dependsOn` within a chunk is correct (§6.4) | Someone uses ordered batches across a chunk boundary |
| Per-request timeout parameter | The middleware's defaults cover the common case | A caller needs to bound one slow call differently from the rest |

---

## 10. Testing strategy

`unittest` with `IsolatedAsyncioTestCase`, both stdlib, so there is no test dependency beyond the
package's own. The seam is `httpx.MockTransport`, wrapped by the **real** `msgraph-core`
middleware, so tests exercise the same retry and redirect path production does (D11).

**188 tests at 94% line coverage**, in eight files:

| File | Covers |
|---|---|
| `test_transport.py` | The pipeline actually engages; the bearer token is attached for Graph and withheld elsewhere; the concurrency gate holds; 429 with `Retry-After` is retried and the delay honoured |
| `test_requests.py` | URL building, absolute passthrough, the scheme-separator rule, OData mapping, request-header forwarding, `Authorization` rejection, response allowlisting |
| `test_operations.py` | Paging, chunking at 20, re-ordering by `id`, a failing sub-request not failing its siblings, the 4 MiB boundary, chunk alignment, download temp-then-rename |
| `test_resources.py` | The exact `sendMail` and `event` payloads, field by field. This is where bugs would otherwise hide |
| `test_files_teams_users.py` | Drive addressing by path and by id, that a large upload still reaches `createUploadSession`, the `chatMessage` shape, and that `ConsistencyLevel` is re-sent on page two |
| `test_signin.py` | The two-phase orchestration: the code returns without waiting; a sign-in that fails before issuing a code does not hang; cancellation; PKCE conformance; the verifier never appearing in the authorize URL; `state` mismatch |
| `test_redirect_and_construction.py` | The parts of sign-in needing no tenant: the loopback listener against real sockets, the PKCE code exchange over a stubbed MSAL, and every way of building a client |
| `test_errors_and_logging.py` | Graph error JSON → `GraphError`, `Retry-After` in both formats, chain flattening, the code table, and that no token, secret or header reaches a log line |

**Live tests** need a tenant and are skipped without one, so a clean checkout never requires
credentials to go green.

---

## 11. Current state

The package is complete and tested. Everything in §5 through §8 is implemented and covered.

**What cannot be verified without a tenant**

- Whether each sign-in flow completes against real Entra. The orchestration around the credential
  is tested with a stub; the credential itself is `azure-identity`'s.
- Whether the permissions each resource declares are sufficient in practice.
- The batching speed claim in §6.4, which is arithmetic from Graph's documented limits rather than
  a measurement.

Set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` and the live checks become
available.

---

## Appendix: the guiding principle, applied

CLAUDE.md asks for *the smallest reliable package that makes Microsoft Graph plug and play*. Three
consequences worth stating, because each one cost something:

**Smallest** meant deleting a working 5,804-line C# implementation once nothing consumed it, and
choosing `msgraph-core` over `msgraph-sdk` on a measurement rather than on which was more official.

**Reliable** meant that the interesting tests are the ones asserting a *negative* — the verifier not
in the URL, `Authorization` not in the response, the token not sent off-host, the log line without
the query string. Those are the properties that fail silently, so they are the ones written down.

**Plug and play** meant the resource layer. A transport port alone would have left `sendMail` as
twenty lines of nested JSON for every caller to rediscover, which is the actual barrier — not
authentication, which `azure-identity` had already solved.
