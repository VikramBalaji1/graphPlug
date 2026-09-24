# Using msgraph_simple from Python

Import one class, hand it credentials, call Graph. Everything behind that — token acquisition and
refresh, retry, throttling, pagination, batching, chunked uploads, error shaping — happens in a
compiled C# core. This layer has no dependencies at all.

> **Before you start.** The wheel is `linux-x64` only and bundles a compiled library, so it will
> not install on Windows or macOS. Run under WSL, a container, or Linux. If you have no wheel yet,
> see [Building it](#building-it) at the end.

---

## Contents

- [Install](#install)
- [Pick an access model](#pick-an-access-model) — the one decision that matters
- [Signing in](#signing-in)
- [Making requests](#making-requests)
- [Paging](#paging)
- [Batching](#batching)
- [Files](#files)
- [Errors](#errors)
- [Logging](#logging)
- [Threads and lifetime](#threads-and-lifetime)
- [Full reference](#full-reference)
- [Things that will catch you out](#things-that-will-catch-you-out)
- [Building it](#building-it)

---

## Install

```bash
pip install msgraph_simple-0.1.0-py3-none-manylinux_2_34_x86_64.whl
```

```python
from msgraph_simple import GraphClient, GraphError
```

Three names are exported: `GraphClient`, `GraphError` and `PendingSignIn`. That is the whole API.

---

## Pick an access model

This is the only decision with consequences. Get it wrong and a script has far more reach than you
intended.

| | **Application-level** | **Delegated** |
|---|---|---|
| Acting as | The application itself | A signed-in person |
| Reach | **The whole tenant** | Only what that person can already do |
| `/me` works | No — there is no user | Yes |
| Human needed | No | Yes, at first sign-in |
| Entra registration | Confidential client, holds a secret | Public client, holds none |
| Constructor | `app_only`, `from_env` | `device_code`, `interactive` |

`Mail.Read` as an **application** permission reads every mailbox in the tenant. The same name as a
**delegated** permission reads only the signed-in person's mail.

These are normally two separate Entra app registrations. Setup for each is in
[ARCHITECTURE.md §7.2](ARCHITECTURE.md#72-entra-app-registration).

---

## Signing in

### Application-level, from the environment

The usual path for a job or a daemon. Reads `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and
`AZURE_CLIENT_SECRET`, and names any that are missing.

```python
with GraphClient.from_env() as graph:
    users = graph.get("/users", select="id,mail")
```

### Application-level, explicit

```python
graph = GraphClient.app_only(
    tenant_id="contoso.onmicrosoft.com",
    client_id="...",
    client_secret="...",
    scopes=None,                 # defaults to .default, which is what you want
    authority_host=None,         # set only for a sovereign cloud
    max_retries=None,            # see "Throttling" below
    max_delay_seconds=None,
)
```

Creating a client does **not** contact Entra. The credential is built locally and the first token
is fetched on the first request, so a bad secret surfaces then rather than here.

### Delegated, device code

For headless boxes and containers. Prints Microsoft's own instruction text, then blocks until the
person finishes on another device.

```python
with GraphClient.device_code(
    tenant_id="...", client_id="...", scopes=["User.Read", "Mail.Read"],
) as graph:
    print(graph.get("/me")["displayName"])
```

To render your own prompt, use the two-phase form:

```python
flow = GraphClient.begin_device_code(tenant_id, client_id, ["User.Read"])

print(f"Go to {flow.verification_uri} and enter {flow.user_code}")
print(f"The code expires in {flow.expires_in // 60} minutes.")

graph = flow.complete()      # blocks until they finish
# flow.cancel()              # if you give up instead
```

`PendingSignIn` carries `flow_id`, `user_code`, `verification_uri`, `message`, `authorize_url`,
`state` and `expires_in`. Which of those are set depends on the flow.

### Delegated, browser

Opens a browser, listens on loopback for the redirect, and returns a ready client.

```python
with GraphClient.interactive(
    tenant_id="...", client_id="...",
    scopes=["User.Read"],
    redirect_uri="http://localhost:8400",
    timeout_seconds=900,
) as graph:
    ...
```

The redirect URI must match your app registration **character for character**. A registered
`http://localhost:8400` and a supplied `http://localhost:8400/` are different values to Entra and
produce AADSTS50011.

**Delegated scopes are never guessed.** Omit `scopes` and you get an error naming the field. There
is no safe default: `.default` on a delegated flow silently requests every scope ever consented for
that client.

---

## Making requests

Once a client exists, **nothing about the request surface depends on how you authenticated.** You
can switch a script between access models by changing one constructor call.

```python
graph.get("/users/alice@contoso.com")
graph.post("/users", body={"displayName": "Alice", ...})
graph.patch("/users/{id}", body={"jobTitle": "Engineer"})
graph.delete("/users/{id}")
```

`get`, `post`, `patch` and `delete` return the **response body**. There is no `put`; uploads have
their own method.

### OData options are plain keywords

```python
graph.get("/users",
          select="id,displayName,mail",
          filter="accountEnabled eq true",
          top=999,
          orderby="displayName")
```

`select`, `filter`, `top`, `skip`, `expand`, `orderby`, `search` and `count` become `$select`,
`$filter` and so on. Anything else is passed through as a literal query parameter:

```python
graph.get("/users/delta", deltaToken="abc")     # -> ?deltaToken=abc
```

You never URL-encode anything; the core does it.

### The whole envelope, headers, beta

`request` returns everything rather than just the body:

```python
response = graph.request(
    "GET", "/users",
    version="beta",                                 # v1.0 unless you say otherwise
    headers={"ConsistencyLevel": "eventual"},
    timeout_ms=30_000,                              # default is 100_000
    select="id",
)

response["status"]     # 200
response["headers"]    # only an allow-listed set; never Authorization
response["body"]
response["nextLink"]   # present only when there is another page
```

You cannot supply an `Authorization` header — it is rejected before the request leaves. The core
owns authentication, and accepting one would bypass the credential your client was built with.

`path` may also be a full URL, in which case `version` and the OData options are ignored because
the URL already carries them. That is what makes echoing a `nextLink` back work.

---

## Paging

A generator. It fetches one page at a time and stops when you do.

```python
for user in graph.paged("/users", select="id,mail", top=999):
    print(user["mail"])
```

Break out early and nothing leaks — there is no cursor state in the native library, so an
abandoned generator holds nothing.

```python
first_ten = list(itertools.islice(graph.paged("/users"), 10))
```

---

## Batching

Many requests as one call. The core splits at Graph's limit of 20 and puts the results back in the
order you sent them, so they line up positionally with your input.

```python
results = graph.batch([
    ("GET", "/users"),
    ("GET", "/groups"),
    {"method": "POST", "url": "/users", "body": {"displayName": "Alice"}},
])
```

A tuple is `(method, url)`; a dict is passed through so you can add a body or headers. Either way
an `id` is assigned for you if you do not supply one.

**Sub-request failures are not raised.** One failure must not discard nineteen successes, so each
result carries its own status and you check them:

```python
for i, result in enumerate(results):
    if not 200 <= result["status"] < 300:
        print(f"request {i} failed: {result['status']}", result["body"].get("error"))
```

A `dependsOn` chain must stay inside one group of 20 — a dependency spanning a chunk boundary
fails at Graph.

---

## Files

### Download

Streams straight to disk. The bytes never enter a JSON envelope and never fully enter memory, so
size is bounded by disk rather than RAM.

```python
result = graph.download("/me/drive/items/{id}/content", "local.bin")
result["bytesWritten"]
result["destPath"]
```

The destination **directory must already exist** — the core will not create it. A failed or
cancelled download leaves nothing behind: it writes to a temporary name and renames only on
success.

```python
Path(dest).parent.mkdir(parents=True, exist_ok=True)
graph.download(path, dest)
```

### Upload

```python
result = graph.upload("/me/drive/root:/big.zip:/content", "big.zip")
result["bytesSent"]
result["body"]["id"]
```

Below 4 MiB this is a single `PUT`. At or above it the core switches to a resumable upload session
in 10 MiB chunks, on its own — you write the same call either way and never learn which ran.

Large transfers can outlast the default 100-second timeout:

```python
graph.upload(path, "big.zip", timeout_ms=600_000)
```

---

## Errors

One exception type carrying data, rather than a hierarchy. You branch on `status` and `code`.

```python
try:
    graph.get("/users/nope")
except GraphError as e:
    e.status        # HTTP status, or 0 when there was no HTTP response at all
    e.code          # "itemNotFound", or a core code such as "timeout"
    e.message
    e.request_id    # quote this to Microsoft support
    e.retry_after   # seconds, when Graph said so
    e.inner         # Graph's own inner error, preserved verbatim
```

`status` is **0** for failures with no response behind them: a timeout, a DNS failure, a bad
secret, a malformed call.

### Throttling

Usually already handled — the pipeline honours `Retry-After` and retries. Catching
`activityLimitReached` means the retry budget ran out:

```python
except GraphError as e:
    if e.code == "activityLimitReached" and e.retry_after:
        time.sleep(e.retry_after)
```

Microsoft throttles per application and per tenant rather than banning an IP, and honouring
`Retry-After` is what keeps you in good standing. **Do not substitute a shorter delay.** If you hit
this constantly, fetch less: `select` only the fields you use, raise `top`, and batch related calls.

To make a request give up sooner rather than sit in a retry loop:

```python
graph = GraphClient.app_only(..., max_retries=2, max_delay_seconds=30)
```

`max_retries` is attempts after the first. `max_delay_seconds` caps the **total** time spent
retrying one request, not the per-attempt wait — that stays whatever Graph asked for.

Every code, its cause and its fix is in [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Logging

Off unless you ask. One JSON object per line on **stderr**, so it will not corrupt anything you
write to stdout.

```bash
MSGRAPH_LOG_LEVEL=info python your_script.py
```

```
{"level":"info","event":"sessionCreated","handle":1}
{"level":"info","event":"request","method":"GET","url":"https://graph.microsoft.com/v1.0/users","status":200,"ms":214,"requestId":"a1b2c3d4","errorCode":null}
```

`error` logs failures only; `info` adds successes and session lifecycle. Anything else, including a
typo, means off.

URLs are logged **without their query string** — an OData `$filter` routinely carries email
addresses — and headers, bodies and credential material are never logged. Correlate with
`requestId`.

---

## Threads and lifetime

A client is safe to share across threads. `HttpClient` is thread-safe, token refresh is serialised
internally, and the handle registry is concurrent. Threads sharing one client also share a
connection pool and a token cache, which is what you want.

Creating one per thread works but is wasteful — and for delegated access it is worse than
wasteful, because each would prompt its own sign-in.

```python
with GraphClient.from_env() as graph:              # closes on exit, including on an exception
    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(lambda uid: graph.get(f"/users/{uid}"), user_ids)
```

Using a client after `close()` raises `invalidHandle` without crossing the boundary. Closing twice
is harmless.

For `asyncio`, wrap calls in `asyncio.to_thread` — `ctypes` releases the GIL for the duration of a
call, so other threads keep running.

---

## Full reference

### `GraphClient` constructors

| | |
|---|---|
| `app_only(tenant_id, client_id, client_secret, scopes=None, authority_host=None, max_retries=None, max_delay_seconds=None)` | Application-level |
| `from_env(**overrides)` | Application-level from `AZURE_*` variables |
| `device_code(tenant_id, client_id, scopes, authority_host=None)` | Delegated, blocks |
| `begin_device_code(tenant_id, client_id, scopes, authority_host=None)` | Delegated, returns a `PendingSignIn` |
| `interactive(tenant_id, client_id, scopes, redirect_uri="http://localhost:8400", authority_host=None, timeout_seconds=900)` | Delegated, browser |

### `GraphClient` methods

| | Returns |
|---|---|
| `request(method, path, version=None, body=None, headers=None, timeout_ms=None, **odata)` | The whole envelope |
| `get(path, **odata)` · `post(path, body=None, **odata)` · `patch(...)` · `delete(...)` | The response body |
| `paged(path, **odata)` | A generator of items |
| `batch(requests, **odata)` | A list of per-request results |
| `download(path, dest_path, **odata)` | `{bytesWritten, destPath, status, headers}` |
| `upload(path, source_path, **odata)` | `{bytesSent, body, status, headers}` |
| `close()` | — |

### `GraphError`

`status` · `code` · `message` · `request_id` · `retry_after` · `inner`

### `PendingSignIn`

`flow_id` · `user_code` · `verification_uri` · `message` · `authorize_url` · `state` ·
`expires_in` · `complete(**completion)` · `cancel()`

---

## Things that will catch you out

- **`/me` does nothing useful under application access.** There is no signed-in person. Use
  `/users/{id}`.
- **A 403 on something the person can evidently do** usually means delegated scopes. Effective
  rights are the intersection of what you asked for and what they could already do.
- **Application permissions need admin consent** and are inert without it. No amount of retrying
  helps.
- **`get` returns the body, `request` returns the envelope.** If you want `nextLink` or the status,
  use `request`.
- **Batch failures are in the results, not in an exception.**
- **The download directory must exist.**
- **A wheel will not install outside Linux.** That is deliberate, not a bug.

---

## Building it

No wheel yet? It must be built on Linux — the compiled core cannot be produced on Windows.

```bash
docker build -f build/Dockerfile -t msgraph-core-build .
docker run --rm -v "$PWD/python/msgraph_simple/_lib:/dest" \
       msgraph-core-build cp /out/MicrosoftGraph.so /dest/

python -m build --wheel python/ \
       -C--build-option=--plat-name=manylinux_2_34_x86_64
```

The container runs the C# test suite before producing anything, so the library never comes out of
code that does not pass.

Working examples live in [samples/](samples). The design, and why any of this is shaped the way it
is, is in [ARCHITECTURE.md](ARCHITECTURE.md).
