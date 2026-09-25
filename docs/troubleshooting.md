# Troubleshooting

Every failure arrives as a `GraphError` carrying a `status` and a `code`. Find the code below.

```python
except GraphError as e:
    print(e.status, e.code, e.message, e.request_id)
```

`status` is the HTTP status, or **0** when there was no HTTP response at all — a timeout, a DNS
failure, a bad secret, a malformed call.

---

## Sign-in

### `consentRequired`  (AADSTS65001)

No consent grant exists for a permission you asked for.

- **Application access:** an administrator must press *Grant admin consent* in the app
  registration. Application permissions are inert without it — no amount of retrying helps.
- **Delegated access:** the person can usually consent themselves at first sign-in. If the scope
  is admin-only, an administrator has to consent on the tenant's behalf.

### `signInDeclined`  (AADSTS65004, `authorization_declined`, `access_denied`)

Somebody pressed *No*. Nothing to fix in code.

### `signInTimeout`  (AADSTS70016, `expired_token`)

The device code expired, or nobody finished within the 15-minute window. Start a new flow —
codes are single-use and short-lived by design.

### `stateMismatch`

The value the browser redirect carried did not match the one issued. Either two sign-ins are
racing on the same redirect port, or something interfered with the redirect. It is validated
inside `interactive()` rather than by the caller, so this check cannot be skipped.

### `interactionRequired`

Two different causes:

1. A delegated flow was reached through `app_only()`. Use `device_code()` or `interactive()`.
2. A delegated session can no longer refresh — the refresh token was revoked, expired, or the
   Conditional Access policy changed. The person has to sign in again, which means a new client.

### `invalidRequest`: `'scopes' is required for delegated authentication`

Delegated flows will not guess. There is no safe default: `.default` in a delegated flow
silently requests every scope ever consented for that client, which is the opposite of least
privilege. Name what you need.

### `authenticationFailed`

The catch-all. Read `e.message` — the AADSTS number is in there, because the package flattens the
whole exception chain rather than reporting Azure.Identity's empty outer message. Usual causes:
a wrong or expired client secret, the wrong tenant, or a client ID that does not exist.

### Browser sign-in hangs, no redirect arrives

The listener binds to `127.0.0.1` on the redirect URI's port and accepts exactly one request.

- The port must be free.
- The redirect URI must match the app registration **character for character**. A registered
  `http://localhost:8400` and a supplied `http://localhost:8400/` are different values to Entra
  and produce AADSTS50011.
- On a headless box there is no browser. The URL is printed; open it elsewhere, or use device code.

---

## Requests

### `activityLimitReached` / status 429

You are being throttled. This is usually already handled — the pipeline honours `Retry-After` and
retries. Seeing it means the retry budget ran out.

```python
except GraphError as e:
    if e.retry_after:
        time.sleep(e.retry_after)
```

Microsoft throttles per application and per tenant; honouring `Retry-After` is what keeps you in
good standing. Do not add your own shorter delay — it makes things worse. If you are hitting this
constantly, fetch less: `select` only the fields you use, raise `top`, and batch related calls.

### `invalidRequest`: `'Authorization' may not be supplied`

You passed an `Authorization` header. The client owns authentication, and accepting one would
silently bypass the credential your client was built with. Remove it.

### `invalidHandle`

The client was closed. A `with` block closes on exit, including when the body raises — so this
usually means a client escaped its block. Create a new one.

### `itemNotFound` on a path that exists

Check the access model. Under application-level access there is no signed-in person, so `/me` and
everything under it does not resolve. Use `/users/{id}` instead.

### Status 403 on something the person can do

Under **delegated** access the effective rights are the intersection of the scopes you requested
and what that person can already do. Asking for `Mail.Read` does not grant access to a mailbox
they could not otherwise open.

Under **application** access the permission is tenant-wide, so a 403 usually means the permission
was never granted or never admin-consented.

### `transportError`

DNS, TLS or a connection reset — below Graph entirely. Check egress, proxies and TLS interception.

### `timeout`

The HTTP client gave up: 30 seconds to connect, 100 seconds to read or write, which are
msgraph-core's defaults. Large uploads already go in 10 MiB chunks, so each request stays well
inside that; a timeout usually means a slow or congested link rather than a big file.

There is no per-request timeout parameter (ARCHITECTURE.md §9). To bound a call yourself, wrap it:

```python
await asyncio.wait_for(graph.files.upload("big.zip"), timeout=600)
```

---

## Files

### `invalidRequest`: `the destination directory … does not exist`

The package does not create directories. Make it yourself:

```python
Path(dest).parent.mkdir(parents=True, exist_ok=True)
g.download(path, dest)
```

### A download failed and left nothing behind

That is deliberate. Downloads write to a temporary sibling file and rename on success, so a
failed or cancelled transfer cannot leave a truncated file at the destination path.

### An upload of a large file is slow

Chunks are 10 MiB and are sent sequentially. Concurrent chunk dispatch is a deliberate omission —
add it if you measure it as a problem.

---

## Batching

### One sub-request failed and I did not notice

Sub-request failures are **not** raised. A batch returns a list, each entry with its own `status`,
because one failure must not discard nineteen successes. Check each one.

### `dependsOn` did not work

Dependency chains are passed through but not validated across a chunk boundary. A batch larger
than 20 is split, and a dependency spanning two chunks will fail at Graph. Keep dependent
requests within a single group of 20.

---

## Diagnostics

Nothing is logged unless you ask:

```bash
GRAPHPLUG_LOG_LEVEL=info python your_script.py
```

`error` logs failures only; `info` adds successful requests and session lifecycle. Output is one
JSON object per line on **stderr**, so it will not corrupt anything you write to stdout.

URLs are logged without their query string — an OData `$filter` routinely carries email addresses
— and headers, bodies and credential material are never logged at all. Correlate with
`requestId`; that is the value Microsoft support asks for.
