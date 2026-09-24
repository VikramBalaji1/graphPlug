# MicrosoftGraph

A minimal C#/.NET core for Microsoft Entra ID authentication and Microsoft Graph request handling,
compiled to a native shared library and consumed from Python through `ctypes`.

Supports both Entra access models — **application-level access** (app-only, application permissions)
and **delegated access** (on behalf of a signed-in user, delegated permissions).

> The design, and the reasoning behind every decision here, lives in [ARCHITECTURE.md](ARCHITECTURE.md).
> This file is how to use and build the thing.

---

## Why

Microsoft already ships `msgraph-sdk` and `azure-identity` for Python. If the only goal were "call
Graph from Python", `pip install msgraph-sdk` would be the whole architecture.

The reason to route through C# is that **the behaviour has one implementation**. Auth handling, retry
semantics, error shape, pagination and batch chunking are written once, tested once in xUnit, and
exposed identically to every consumer. A .NET service and a Python script cannot drift apart, because
they are the same compiled code.

The price is a foreign-function boundary, NativeAOT constraints, and per-platform builds. If that
stops being worth it, the correct move is to delete this package and use `msgraph-sdk` directly.

---

## Quick start

### Python

```python
from msgraph_simple import GraphClient, GraphError

# Application-level access — app permissions, no user
with GraphClient.app_only(tenant_id=..., client_id=..., client_secret=...) as g:
    for user in g.paged("/users", select="id,displayName,mail"):
        print(user["mail"])

# Delegated access — a real user signs in, their permissions apply
with GraphClient.device_code(tenant_id=..., client_id=..., scopes=["User.Read"]) as g:
    print(g.get("/me")["displayName"])
```

Once a `GraphClient` exists, **nothing about the request surface depends on how it was
authenticated.** Scripts switch between access models by changing one constructor call.

```python
g.get("/users/alice@contoso.com")
g.get("/users", select="id,mail", filter="accountEnabled eq true", top=999)
g.post("/users", body={...});  g.patch("/users/{id}", body={...});  g.delete("/users/{id}")
g.request("GET", "/users", version="beta", headers={"ConsistencyLevel": "eventual"})

for user in g.paged("/users", select="id,mail"):
    ...

g.download("/me/drive/items/{id}/content", "local.bin")
g.upload("/me/drive/root:/big.zip:/content", "big.zip")
results = g.batch([("GET", "/users"), ("GET", "/groups")])

try:
    g.get("/users/nope")
except GraphError as e:
    print(e.status, e.code, e.request_id)
```

### C#

The core is `internal` by design — it is consumed across the C ABI, not referenced as a library.
The NuGet package exists for .NET consumers who want the same compiled behaviour; see
[ARCHITECTURE.md §6](ARCHITECTURE.md#6-the-abi-contract) for the nine exported entry points.

---

## The two access models

Choosing the wrong one is how an automation script ends up with far more reach than intended.

| | **Application-level** | **Delegated** |
|---|---|---|
| Acting as | The application itself | A signed-in user |
| Entra permission type | Application permissions | Delegated permissions |
| Consent | Admin, once, tenant-wide | User (or admin) |
| Effective rights | The granted app permissions, **across the whole tenant** | Intersection of the scopes and what that user can already do |
| `/me` works | No | Yes |
| Scope requested | `https://graph.microsoft.com/.default` | Explicit: `User.Read`, `Mail.Send`, … |
| Human needed | No | Yes, at first sign-in |
| Client type | Confidential (holds a secret) | Public (holds no secret) |

`Mail.Read` as an **application** permission reads every mailbox in the tenant. The same name as a
**delegated** permission reads only the signed-in user's mail.

These are normally **two separate app registrations**. Setup for each is in
[ARCHITECTURE.md §7.2](ARCHITECTURE.md#72-entra-app-registration).

### Supported flows

| Flow | Access model | Status |
|---|---|---|
| Client secret | Application | Supported |
| Device code | Delegated | Supported |
| Authorization code + PKCE | Delegated | Supported |
| Client certificate | Application | Extension point ready |
| Managed identity | Application | Extension point ready |
| On-behalf-of | Delegated | Extension point ready |
| Interactive browser / WAM | Delegated | Excluded — not NativeAOT-compatible |
| ROPC (username/password) | Delegated | Excluded — breaks under MFA, stores passwords |

Adding one of the "extension point ready" rows is a new `AuthenticationStrategy` subclass and one
case in one factory. Nothing else in the codebase moves.

---

## What the core handles for you

- **Tokens.** Acquisition, caching, expiry and refresh are entirely Azure.Identity's job. This
  package writes no token-handling code — no manual `/oauth2/v2.0/token` calls, no expiry
  arithmetic, no refresh timer.
- **Retry and throttling.** Microsoft's supported handler pipeline. `429`, `503` and `504` are
  retried and `Retry-After` is honoured. No retry logic is written here. When the budget is
  exhausted, the failure still reports `retryAfterSeconds` so you can back off yourself.
- **Pagination.** `@odata.nextLink` is lifted to the top of the response; Python owns the loop, so
  there is no cursor state in the native library to leak.
- **Batching.** Chunked at Graph's limit of 20, and re-ordered by `id` so results align with what you
  sent. One failing sub-request does not discard its siblings.
- **Files.** Downloads stream to disk and never enter memory or a JSON envelope. Uploads switch to a
  chunked upload session above 4 MiB.
- **Errors.** One shape for every failure — transport, auth, Graph or a bug in the core.

---

## Security properties

These are deliberate, and they are tested rather than documented and hoped for.

- **No export returns an access token or a refresh token.** There is no raw-token escape hatch.
- **Response headers pass an allowlist, never a denylist.** A denylist fails open on whatever header
  Microsoft adds tomorrow. `Authorization` and `WWW-Authenticate` cannot escape even by accident.
- **A caller-supplied `Authorization` header is rejected.** The core owns authentication; accepting
  one would silently bypass the credential the handle was created with.
- **Delegated scopes are never defaulted.** `.default` in a delegated flow silently requests every
  scope ever consented for that client — the opposite of least privilege. Omitting `scopes` is an
  error naming the missing field.
- **The PKCE verifier never crosses the ABI.** It stays inside the core for the life of the
  exchange, so Python cannot leak it by logging a return value. `state` is validated in the core,
  not in Python, so the CSRF check cannot be skipped by a caller reimplementing the loop.
- **Absolute URLs are allowed but unauthenticated off-host.** Pre-authenticated download URLs work;
  the bearer token is withheld from any host but `graph.microsoft.com`.
- **Nothing is written to disk.** The token cache is in-memory for the life of the handle, so there
  is no credential material at rest and no keyring dependency.

> **Known ceiling.** `ClientSecretCredential` takes a `string`, and a managed string cannot be
> reliably zeroed. A process memory dump can therefore contain the secret. Certificate auth or
> managed identity removes the shared secret entirely. Delegated flows are better off by
> construction: a public client holds no secret at all.

---

## Errors

Every failure produces the same envelope, and `GraphError` carries it into Python:

```python
except GraphError as e:
    e.status        # HTTP status, or 0 for non-HTTP failures
    e.code          # Graph error code, or a core code (below)
    e.message
    e.request_id    # quote this to Microsoft support
    e.retry_after
    e.inner         # Graph's innerError, preserved verbatim
```

Failures with no HTTP response use `status: 0` and a core-defined code:

| Code | Cause |
|---|---|
| `authenticationFailed` | Bad secret, wrong tenant, expired credential |
| `consentRequired` | A requested delegated scope has not been consented to |
| `interactionRequired` | A two-phase flow was used through the single-shot path |
| `signInTimeout` | The user did not complete sign-in within the window |
| `signInDeclined` | The user cancelled, or Entra denied the sign-in |
| `stateMismatch` | The auth-code redirect `state` did not match — a possible CSRF attempt |
| `transportError` | DNS, TLS, connection reset |
| `timeout` | `timeoutMs` elapsed |
| `invalidRequest` | Malformed envelope, or a delegated credential missing `scopes` |
| `invalidHandle` | Handle unknown or already closed |
| `unsupportedCredentialType` | Unknown `type` in the credentials envelope |
| `requestFailed` | A retry budget was exhausted with no readable Graph error |
| `internalError` | A bug in the core |

---

## Logging

Off unless asked. Set `MSGRAPH_LOG_LEVEL` to `error` or `info`; anything else, including a typo,
means off. Output is one JSON object per line on **stderr**, because a caller may be piping stdout.

```console
$ MSGRAPH_LOG_LEVEL=info python my_script.py
{"level":"info","event":"sessionCreated","handle":1}
{"level":"info","event":"request","method":"GET","url":"https://graph.microsoft.com/v1.0/users","status":200,"ms":214,"requestId":"a1b2c3d4","errorCode":null}
{"level":"error","event":"request","method":"GET","url":"https://graph.microsoft.com/v1.0/users/nope","status":404,"ms":88,"requestId":"e5f6a7b8","errorCode":"itemNotFound"}
```

| Level | Emits |
|---|---|
| `off` (default) | Nothing |
| `error` | Failed requests and failures at the ABI boundary |
| `info` | The above, plus successful requests and session lifecycle |

**URLs are logged without their query string**, because an OData `$filter` routinely carries user
identifiers and a `nextLink` carries a skiptoken. Headers, request bodies and credential material are
never logged — the logger takes only the exact fields it may emit, so there is no overload through
which anything else could reach a line.

Correlate with `requestId`; it is the value Microsoft support asks for.

---

## Development

The development machine is Windows. The shipped artefact is a `linux-x64` shared library.

```bash
dotnet build                        # host build, fast feedback
dotnet test                         # unit + integration tests, Windows or Linux
dotnet format                       # style
dotnet pack -c Release              # NuGet artefact for .NET consumers

docker build -f build/Dockerfile -t msgraph-core-build .        # linux-x64 .so
docker run --rm -v "$PWD/python/msgraph_simple/_lib:/dest"        msgraph-core-build cp /out/libMicrosoftGraph.so /dest/   # bundle it into the package

python -m unittest discover -s python/tests    # Python layer; ABI tests need the .so above
python -m build python/                        # wheel, run inside Linux
```

### The cross-compilation consequence

**NativeAOT cannot cross-compile from Windows to Linux.** It invokes the platform linker, so a
`linux-x64` binary must be produced on Linux. Two consequences:

1. **The build runs in Docker.** `build/Dockerfile` is the definition of a build; there is no
   "works on my machine" path.
2. **The Python loop lives in Docker or WSL.** Editing happens on Windows; running the Python
   package happens on Linux.

C# tests are exempt — they are plain managed code and run natively on Windows. That keeps the fast
inner loop (write C#, run xUnit) on the host and pushes only the slower outer loop into a container.

### Prerequisites

| For | You need |
|---|---|
| `dotnet build` / `test` / `pack` | .NET 10 SDK |
| `docker build` | Docker, or a Linux box with the .NET 10 SDK plus `clang` and `zlib1g-dev` |
| Running the Python package | Linux, Python 3.9+ |
| Live tests | An Entra tenant and the app registrations in ARCHITECTURE.md §7.2 |

---

## Testing

```bash
dotnet test
```

Four layers, per [ARCHITECTURE.md §13](ARCHITECTURE.md#13-testing-strategy):

| Layer | What it covers | Needs |
|---|---|---|
| **Unit** | URL building, header allowlisting, error mapping, pagination, batching, auth strategies, PKCE, serialisation, logging | Nothing — stubbed transport |
| **Integration** | The real Microsoft handler pipeline: retry, `Retry-After`, claims challenges, token withholding | Nothing — controlled transport, fake credential |
| **ABI** | The built `.so` driven from Python: no-crash cases, memory stability, handle lifecycle | A Linux build |
| **Live** | Real Graph, real tenants | Environment variables; skipped otherwise |

`dotnet test` on a clean checkout never requires a tenant.

Tests run with reflection-based JSON serialisation **disabled**, matching the shipped binary, so a
serialisation path that only works under the JIT fails in CI rather than in production.

---

## Project layout

```text
src/MicrosoftGraph/
├── Authentication/   credential acquisition, one class per Entra flow
├── Diagnostics/      the opt-in structured logger
├── Graph/            session, executor, URL building, operations, upload strategies
├── Interop/          the nine native entry points and the handle registries
└── Models/           the envelopes that cross the ABI, and the error shape

tests/UnitTests/          stubbed transport, no network
tests/IntegrationTests/   the full handler pipeline, controlled responses

python/msgraph_simple/    the ctypes binding — stdlib only, no dependencies
build/Dockerfile          the linux-x64 NativeAOT build
```

---

## Dependencies

| Package | Why |
|---|---|
| `Azure.Identity` | Token acquisition, refresh and in-memory caching |
| `Microsoft.Graph.Core` | The supported handler pipeline, without the Kiota-generated types |
| `Microsoft.Identity.Client` | The PKCE auth-code exchange for a public client (already a transitive dependency of Azure.Identity, so the graph is unchanged) |

The Python layer has **none** — `ctypes`, `json`, `secrets`, `webbrowser` and `http.server`, all
stdlib. A package whose selling point is "plug and play" should not begin with a dependency
resolution.

---

## Deliberate omissions

Each is a decision with a trigger, not an oversight. The full table is in
[ARCHITECTURE.md §14](ARCHITECTURE.md#14-deliberate-omissions). The ones most likely to matter:

- **No persistent token cache.** A new process means a new delegated sign-in. Add one when repeated
  CLI invocations make re-prompting tiresome enough to accept a refresh token on disk.
- **No `graph_cancel` for in-flight requests.** `timeoutMs` covers the common case, and pending
  sign-ins are already cancellable.
- **No typed Graph models.** Generic JSON reaches every `v1.0` and `beta` endpoint on day one.
- **`linux-x64` only.** Adding `win-x64` is a RID change and a second build leg, not a redesign.

---

## Status

All seven milestones of [ARCHITECTURE.md §15](ARCHITECTURE.md#15-build-order) are implemented.
**158 C# tests and 58 Python tests**, with the Release build clean of warnings.

| # | Milestone | Verified by |
|---|---|---|
| 1 | AOT spike | **Partial.** AOT and trim analysers are clean and reflection-based JSON is disabled in the library *and* both test hosts, so the tests run under the shipped binary's constraints. The native link itself is unverified — see below |
| 2 | Core request path, application access | Unit and integration suites, including the `Authorization` assertions |
| 3 | Delegated access | Unit suite including the PKCE and scope-required tests, plus a full device code flow against a stubbed Entra: begin → code issued → complete → a session that calls Graph |
| 4 | Python package | The layer above the boundary is covered; the ABI tests are written and skip until the `.so` exists |
| 5 | Files | A file over 4 MiB round-trips through an upload session with matching SHA-256, and a failed download leaves nothing at the destination |
| 6 | Batching | A 25-request batch returns 25 responses in submission order with one deliberately failing sub-request reported in place |
| 7 | Packaging | `build/Dockerfile` and the CI workflow are written; neither has been executed here |

### What has not been run

NativeAOT cannot cross-compile, and this was developed on Windows with no Docker and no Linux .NET
SDK. So the following are **written but unexecuted**, and should be treated as unproven until a
Linux build runs:

- the NativeAOT link itself, and therefore the `.so`
- every ABI test (19 of them, currently skipping)
- the Docker build, the wheel, and the CI workflow
- anything requiring a real tenant, including whether Entra accepts the secretless PKCE exchange

`dotnet test` and the Python layer tests are green and need none of the above.

## Licence

Not yet chosen.
