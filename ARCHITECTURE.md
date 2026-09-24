# Technical Architecture

A minimal C#/.NET core for Microsoft Entra ID authentication and Microsoft Graph request handling,
compiled to a native shared library and consumed from Python through `ctypes`.

Supports both Entra ID access models — **application-level access** (app-only, application permissions)
and **delegated access** (on behalf of a signed-in user, delegated permissions).

---

## 1. Purpose and scope

Microsoft Graph and Azure Identity are already simple. This package exists to make them *plug and play*
from Python: import one class, hand it your credentials, and call Graph.

```python
from msgraph_simple import GraphClient

# Application-level access — app permissions, no user
with GraphClient.app_only(tenant_id=..., client_id=..., client_secret=...) as g:
    for user in g.paged("/users", select="id,displayName,mail"):
        print(user["mail"])

# Delegated access — a real user signs in, their permissions apply
with GraphClient.device_code(tenant_id=..., client_id=..., scopes=["User.Read"]) as g:
    print(g.get("/me")["displayName"])
```

Everything behind that — credential construction, token acquisition and refresh, sign-in flow
orchestration, the retry and throttling pipeline, pagination, batching, chunked uploads, error
normalisation — lives in C#. The Python layer is a thin, dependency-free binding.

**In scope for v1**

- **Application-level access:** client-credentials (client secret) authentication.
- **Delegated access:** device code flow, and authorization code flow with PKCE.
- Generic authenticated requests to any Microsoft Graph `v1.0` or `beta` endpoint.
- OData query parameters, custom headers, request bodies, per-request timeouts.
- Pagination, batching, file download, file upload (including large-file upload sessions).
- Consistent, structured error information.
- Retry and throttling via Microsoft's supported handler pipeline.
- A `linux-x64` native binary packaged as a Python wheel.

**Out of scope for v1** — see §14 for each omission and the trigger that should bring it in.

---

## 2. Why a C# core

This is a deliberate architectural choice, and it is worth recording the cost honestly.

Microsoft ships first-party Python packages — `msgraph-sdk` and `azure-identity` — that already do what
this package does. If the only goal were "call Graph from Python", `pip install msgraph-sdk` would be the
whole architecture.

The reason to route through C# is that **the behaviour has one implementation**. Auth handling, retry
semantics, error shape, pagination and batch chunking are written once, tested once in xUnit, and exposed
identically to every consumer. CLAUDE.md states this directly: *"The C# implementation is the source of
truth for authentication and Microsoft Graph behavior. The future Python interface consumes this C# core
rather than reimplementing authentication or Graph functionality."*

> **Revised once the code existed.** This section originally rested on a second argument: that a future
> .NET service and the Python scripts could not drift apart, being the same compiled code. There is no .NET
> service, and none is planned, so that argument is withdrawn rather than left standing as a promise the
> package does not keep. The core is not published as a NuGet package; every type in it is `internal` and
> the only consumer is the Python wheel, which loads the compiled library rather than referencing the
> assembly.
>
> What justifies the C# core now is narrower and worth stating plainly: the behaviour is written once
> against Microsoft's own supported libraries, and the guarantees in §6.7, §7.3 and §7.7 are enforced by
> the boundary rather than remembered by a caller. If that stops being worth the four rows below, the
> advice in the paragraph after them still applies.

The price paid for that:

| Cost | Consequence |
|---|---|
| A foreign-function boundary | Everything crosses as UTF-8 JSON or file paths (§6) |
| NativeAOT constraints | Some Azure.Identity flows are unavailable (§10) |
| Per-platform builds | Each target OS/arch needs its own build leg (§12) |
| No cross-compilation | Building for Linux from Windows requires a container (§12) |

If the single-implementation guarantee stops being worth those four rows, the correct move is to delete
this package and use `msgraph-sdk` directly. That is not a defeat; it is the tradeoff resolving the other
way.

---

## 3. Decisions and rationale

| # | Decision | Rationale | Rejected alternative |
|---|---|---|---|
| D1 | **NativeAOT shared library + `ctypes`** | No .NET runtime needed on the target machine; fast startup; a wheel with zero pip dependencies. | *pythonnet* — requires a .NET runtime everywhere and has no Python 3.14 support. *Sidecar process* — clean, but adds process lifecycle, IPC latency and a large self-contained host binary. |
| D2 | **`Microsoft.Graph.Core` only** | Supplies the supported `GraphClientFactory` handler pipeline without the thousands of Kiota-generated types. Small binary, fast AOT compile, every `v1.0` and `beta` endpoint reachable on day one. | The full `Microsoft.Graph` metapackage — typed request builders whose typing does not survive the trip to Python, in exchange for a very large binary and trimming risk. |
| D3 | **Both access models: app-only and delegated** | The package must serve an application-permissions app *and* a delegated-permissions app. These are two different Entra app registrations and two different token stories (§7). | App-only alone — simpler, but leaves every "act as the signed-in user" scenario unreachable. |
| D4 | **Delegated via device code and auth-code+PKCE** | Both are NativeAOT-safe. Device code fits headless scripts and containers; auth-code+PKCE gives a better desktop experience. | *Interactive browser / WAM broker* — not AOT-compatible (§10). *ROPC* — breaks under MFA and Conditional Access, and stores user passwords. |
| D5 | **Two-phase ABI for interactive sign-in** | A sign-in flow must pause for a human. `begin` returns what the user needs, `complete` waits for them. No callback ever crosses back into Python, so there is no GIL re-entrancy hazard and no function pointer to keep alive. | A Python callback passed as a native function pointer — workable, markedly more fragile. |
| D6 | **In-memory token cache only** | No credential material at rest, no `libsecret`/keyring dependency, identical behaviour inside a container. | A persistent cache — better repeat-run UX, at the cost of a refresh token on disk and a Linux keyring dependency that containers usually lack. |
| D7 | **JSON envelopes over the ABI** | The C ABI carries primitives and pointers. One serialisation format used uniformly beats a bespoke struct layout per operation. | Marshalled structs — faster, vastly more code, and every schema change becomes an ABI break. |
| D8 | **Stateless pagination** | `nextLink` is already a complete, self-describing cursor. Returning it lets Python own iteration with zero native state. | Iterator handles — three more exports and a leak surface, to re-implement what the URL already encodes. |
| D9 | **File paths for binary payloads** | A 2 GB download must not become a 2.7 GB base64 string inside a JSON envelope. | Streaming bytes across the ABI in chunks — a second protocol for a problem the filesystem already solves. |
| D10 | **Session handles, not per-call credentials** | Passing credentials on every call would re-acquire tokens constantly, and for delegated access would re-prompt the user. A handle owns one credential, one `HttpClient`, one token cache. | Fully stateless calls taking credentials each time. |
| D11 | **Polymorphism at the variation points** | Multiple auth strategies, multiple upload strategies and multiple operation types are real, enumerable variation. Strategy and template-method structure makes each addition a new class rather than an edit to existing ones. | A procedural core with `switch` statements — smaller today, and the thing every new flow has to cut into. |
| D12 | **`linux-x64` only** | The stated deployment target. One build leg is one build leg. | A three-platform matrix maintained before anything runs on those platforms. |

---

## 4. Design principles

The codebase serves two directives that pull against each other, and the resolution should be explicit
rather than left to taste.

CLAUDE.md asks for *"the smallest reliable C# core"* and warns against abstractions for single-use code.
The project also calls for **object-oriented design for reusability and clean code**. These are reconciled
by one rule:

> **Abstract at real variation points. Stay concrete everywhere else.**

A variation point is real when it is *enumerable and populated*: there are two or more implementations
today, or a named, scheduled third. Speculative extensibility is not a variation point.

**Where polymorphism earns its place:**

| Abstraction | Implementations today | Pattern |
|---|---|---|
| `AuthenticationStrategy` | client secret, device code, auth code + PKCE | Strategy |
| `PendingAuthentication` | device code, auth code | Template method |
| `IUploadStrategy` | simple `PUT`, chunked session | Strategy |
| `GraphOperation` | json request, download, upload, batch | Template method |
| `HandleRegistry<T>` | sessions, pending authentications | Generic reuse |

Each row has at least two concrete members on day one. Each new auth flow (§7.6) is a new subclass and a
new `case` in one factory — no edits to the executor, the pipeline, the ABI or the Python layer. That is
the reusability being bought, and it is measurable: the diff for adding certificate auth is one file.

**Where it does not, and why:**

- **No `IGraphSession` / `IGraphClient` interface.** One implementation, and the seam tests actually need
  is `HttpMessageHandler` — a platform abstraction that already exists. Inventing a parallel interface
  would add a type without adding a seam.
- **No separate request executor.** An earlier draft split `GraphSession` from a `GraphRequestExecutor`
  that held the `HttpClient` and delegated to operations. The session had one real member and the executor
  had two; between them they were one object wearing two names, so they are one class.
- **No DI container.** Seven collaborating classes and an explicit composition root. A container would add
  runtime reflection, which NativeAOT specifically penalises (§10).
- **No repository/unit-of-work layer.** There is no persistence.
- **No abstract base with one subclass, anywhere.** If a hierarchy collapses to one implementation, it
  collapses back to a class.

**Clean-code conventions applied throughout:** immutable `record` types for request/response/error values;
constructor injection with `readonly` fields; `IAsyncDisposable` on anything owning a connection or a
credential; guard clauses over nested conditionals; one reason to change per class; no method longer than
roughly thirty lines; names drawn from the Graph and Entra domain (`DeviceCodeStrategy`,
`ChunkedUploadStrategy`) rather than from patterns (`Helper`, `Manager`, `Util`).

---

## 5. Component map

```text
┌───────────────────────────── Python process ─────────────────────────────┐
│                                                                          │
│  your script                                                             │
│      │                                                                   │
│      ▼                                                                   │
│  msgraph_simple/__init__.py   GraphClient · GraphError · PendingSignIn   │
│      │                        app_only() · device_code() · interactive() │
│      ▼                                                                   │
│  msgraph_simple/_native.py    ctypes bindings, UTF-8 marshalling,        │
│      │                        guaranteed graph_free()                    │
│ ═════╪════════════════════ C ABI · UTF-8 JSON ══════════════════════════ │
│      ▼                                                                   │
│  MicrosoftGraph.so                                                    │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ Interop/Exports.cs        9 [UnmanagedCallersOnly] entrypoints     │  │
│  │ Interop/HandleRegistry<T> sessions · pending authentications       │  │
│  ├────────────────────────────────────────────────────────────────────┤  │
│  │ Graph/GraphSession            aggregate root, one per handle;      │  │
│  │                               owns the transport and dispatches    │  │
│  │ Graph/Operations/*            json · download · upload · batch     │  │
│  │ Graph/Upload/*                simple · chunked strategies          │  │
│  ├────────────────────────────────────────────────────────────────────┤  │
│  │ Authentication/AuthenticationStrategy   (abstract)                 │  │
│  │   ├── AppOnly/ClientSecretStrategy       application permissions   │  │
│  │   ├── Delegated/DeviceCodeStrategy       delegated permissions     │  │
│  │   └── Delegated/AuthorizationCodeStrategy (PKCE)                   │  │
│  │ Authentication/PendingAuthentication    (abstract, two-phase)      │  │
│  ├────────────────────────────────────────────────────────────────────┤  │
│  │ Microsoft.Graph.Core   retry · redirect · compression · auth       │  │
│  │ Azure.Identity         token acquisition, refresh, in-memory cache │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                    │  HTTPS
                                    ▼
                         graph.microsoft.com/{v1.0,beta}
```

### Repository layout

```text
src/MicrosoftGraph/
├── Authentication/
│   ├── AuthenticationStrategy.cs         # abstract: CreateCredentialAsync, CreatePendingAuthentication
│   ├── AuthenticationStrategyFactory.cs  # creds.type -> strategy
│   ├── PendingAuthentication.cs          # abstract: BeginAsync / CompleteAsync
│   ├── AppOnly/
│   │   └── ClientSecretStrategy.cs
│   └── Delegated/
│       ├── DelegatedStrategy.cs          # shared: public client, refuses to default scopes
│       ├── DelegatedSignIn.cs            # the sign-in window, and failure translation
│       ├── DeviceCodeStrategy.cs
│       ├── DeviceCodePendingAuthentication.cs
│       ├── AuthorizationCodeStrategy.cs
│       ├── AuthorizationCodePendingAuthentication.cs
│       ├── MsalAuthorizationCodeCredential.cs   # the PKCE exchange (§10)
│       └── PkceCodes.cs                  # verifier/challenge, never crosses the ABI
├── Diagnostics/
│   └── GraphLog.cs                       # opt-in structured logging (§14)
├── Graph/
│   ├── GraphSession.cs                   # credential + HttpClient + dispatch, IAsyncDisposable
│   ├── GraphUrlBuilder.cs                # version + path + OData query
│   ├── Operations/
│   │   ├── GraphOperation.cs             # abstract template method
│   │   ├── JsonRequestOperation.cs
│   │   ├── DownloadOperation.cs
│   │   ├── UploadOperation.cs
│   │   └── BatchOperation.cs
│   └── Upload/
│       ├── IUploadStrategy.cs
│       ├── SimpleUploadStrategy.cs       # < 4 MiB
│       └── ChunkedUploadStrategy.cs      # >= 4 MiB, upload session
├── Interop/
│   ├── Exports.cs                        # the nine native entrypoints, marshalling only
│   └── HandleRegistry.cs                 # HandleRegistry<T>, used twice
├── Models/
│   ├── GraphErrorInfo.cs                 # the one place a failure becomes an error object
│   ├── GraphCoreException.cs             # also holds the core version constant
│   ├── EntraFailure.cs                   # reads the AADSTS detail off the inner exception
│   ├── ResponseHeaderFilter.cs           # the §6.7 allowlist, shared by two call sites
│   ├── Envelopes/                        # ABI DTOs; request and response records live here
│   └── GraphJsonContext.cs               # source-generated serialisation
└── MicrosoftGraph.csproj

tests/
├── UnitTests/                            # stubbed HttpMessageHandler, no network
└── IntegrationTests/                     # full pipeline, controlled responses; live tests env-gated

python/
├── setup.py                              # tags the wheel py3-none-<platform> (§12.3)
├── pyproject.toml
├── tests/                                # test_client.py (above the ABI) · test_abi.py
└── msgraph_simple/
    ├── __init__.py                       # GraphClient, GraphError, PendingSignIn
    ├── _native.py                        # ctypes layer
    ├── _auth.py                          # browser + localhost redirect listener (stdlib only)
    ├── _lib/MicrosoftGraph.so            # built artefact, bundled into the wheel
    └── py.typed

build/Dockerfile                          # linux-x64 NativeAOT build
.github/workflows/ci.yml                  # managed tests, then the container build and the wheel
docs/troubleshooting.md                   # every error code: cause and fix
samples/                                  # runnable scripts, one per access model
```

`Exports.cs` contains no logic beyond marshalling, dispatch and the mandatory `catch` (§6.2). Every
behavioural decision lives in a class that a unit test can construct directly, without going near the ABI.

---

## 6. The ABI contract

### 6.1 Exports

Nine functions, one convention: **every function returns a pointer to a NUL-terminated UTF-8 JSON
envelope, and the caller must release it with `graph_free`.** No out-parameters, no status codes, no second
convention to remember.

```c
/* single-shot credentials — application-level access */
char* graph_client_create   (const char* creds_json);

/* two-phase sign-in — delegated access */
char* graph_auth_begin      (const char* creds_json);
char* graph_auth_complete   (int64_t flow_id, const char* input_json);
char* graph_auth_cancel     (int64_t flow_id);

/* session lifetime */
char* graph_client_close    (int64_t handle);

/* operations */
char* graph_request         (int64_t handle, const char* req_json);
char* graph_download        (int64_t handle, const char* req_json);
char* graph_upload          (int64_t handle, const char* req_json);

void  graph_free            (char* ptr);
```

C# side — note how thin the export is once the operation hierarchy exists:

```csharp
[UnmanagedCallersOnly(EntryPoint = "graph_request")]
public static IntPtr GraphRequest(long handle, IntPtr reqJson)
{
    try
    {
        var request = RequestEnvelope.Parse(Marshal.PtrToStringUTF8(reqJson));
        var session = SessionRegistry.Get(handle);          // throws if unknown
        using var cts = new CancellationTokenSource(request.TimeoutMs);

        var response = session
            .ExecuteAsync(new JsonRequestOperation(request), cts.Token)
            .GetAwaiter().GetResult();

        return Emit(response);
    }
    catch (Exception ex)
    {
        return Emit(ErrorEnvelope.From(ex));
    }
}
```

### 6.2 The no-throw rule

**No managed exception may escape an `[UnmanagedCallersOnly]` body.** The runtime cannot unwind a managed
exception into native frames; it terminates the process. A single unhandled `NullReferenceException` inside
the core would kill the calling Python interpreter with no traceback and no opportunity to recover.

Every export body is therefore wrapped in `try { … } catch (Exception ex) { return Emit(ErrorEnvelope.From(ex)); }` —
a blanket `catch (Exception)`, not a catch of specific types. This is the one place in the codebase where
catching everything is correct, and it gets a dedicated test (§13).

### 6.3 Async across a synchronous boundary

The exports are synchronous; the implementation is `async` throughout. The boundary blocks with
`.GetAwaiter().GetResult()`. Internally a `CancellationToken` derived from the request's `timeoutMs` is
threaded through every asynchronous call, satisfying CLAUDE.md's requirement that every async operation
accepts and propagates one.

Blocking is safe on the Python side: `ctypes` releases the GIL for the duration of a foreign call, so other
Python threads keep running. `asyncio` callers wrap invocations in `asyncio.to_thread`.

`graph_auth_complete` is the one long block by design — it waits for a human. Its timeout is the sign-in
window (default 15 minutes, matching the device code lifetime), not the request timeout.

### 6.4 Memory ownership

| Direction | Owner | Lifetime |
|---|---|---|
| Python → C# (`const char*`) | Python | Valid only for the duration of the call. C# copies immediately via `Marshal.PtrToStringUTF8` and never retains the pointer. |
| C# → Python (`char*`) | C# allocator | Allocated with `Marshal.StringToCoTaskMemUTF8`. **Python must call `graph_free`**, which calls `Marshal.FreeCoTaskMem`. |

The Python binding never exposes a raw pointer to user code. One helper owns the whole lifecycle:

```python
def _call(fn, *args) -> dict:
    ptr = fn(*args)
    if not ptr:
        raise GraphError(0, "nullPointer", "core returned no envelope")
    try:
        return json.loads(ctypes.string_at(ptr).decode("utf-8"))
    finally:
        _lib.graph_free(ptr)
```

`graph_free(NULL)` is a no-op, so a null free is harmless.

### 6.5 Envelopes

**Credentials** — the `type` field selects the strategy, and the remaining fields are strategy-specific.

```json
// application-level access
{ "type": "clientSecret",
  "tenantId": "…", "clientId": "…", "clientSecret": "…",
  "scopes": ["https://graph.microsoft.com/.default"] }

// delegated access — device code
{ "type": "deviceCode",
  "tenantId": "…", "clientId": "…",
  "scopes": ["User.Read", "Mail.Send"] }

// delegated access — authorization code + PKCE
{ "type": "authorizationCode",
  "tenantId": "…", "clientId": "…",
  "scopes": ["User.Read"],
  "redirectUri": "http://localhost:8400" }
```

`scopes` defaults to `.default` for app-only and is **required** for delegated flows (§7.3).
`authorityHost` is optional on all types, for sovereign clouds.

**Request** — `graph_request`:

```json
{
  "method": "GET",
  "path": "/users",
  "version": "v1.0",
  "query": { "$select": "id,displayName,mail", "$top": 999 },
  "headers": { "ConsistencyLevel": "eventual" },
  "body": null,
  "timeoutMs": 100000
}
```

- `path` may be a relative Graph path *or* an absolute URL. When absolute — the common case being a
  `nextLink` echoed straight back — `version` and `query` are ignored, because the URL already carries them.
- `version` selects the `v1.0` or `beta` base URL. `v1.0` is the default per CLAUDE.md; `beta` is opt-in and
  explicit at the call site.
- `body` is arbitrary JSON, passed through untouched.
- `query` values are URL-encoded by the core. Callers write `$select`, not `%24select`.

**Success response**:

```json
{
  "ok": true,
  "status": 200,
  "headers": { "request-id": "…", "client-request-id": "…", "Date": "…" },
  "body": { "value": [ … ] },
  "nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=…"
}
```

`nextLink` is lifted out of `@odata.nextLink` and promoted to the top level, so Python never needs to know
the OData annotation name. It is absent when there is no further page. `body` is `null` for `204 No Content`.

Error responses are specified in §9.

### 6.6 Versioning the ABI

The export names are the contract. Adding a field to an envelope is backward-compatible; removing or
renaming one is not, and neither is changing an export's signature. If that becomes necessary, add
`graph_request_v2` alongside the original rather than changing it in place.

`graph_client_create` and `graph_auth_complete` both return `coreVersion`. `_native.py` compares it against
the wheel's own version at load time and raises on mismatch — the `.so` and the Python code ship as one
unit, and a mismatched pair must fail loudly rather than subtly.

### 6.7 Header handling at the boundary

Response headers reach Python through an **allowlist**, never a denylist:

```text
request-id · client-request-id · Date · Retry-After · Content-Type · Location · ETag
```

A denylist fails open: a header Microsoft adds tomorrow would be returned by default. An allowlist fails
closed. `Authorization` and `WWW-Authenticate` therefore cannot reach Python even by accident.

Request headers are forwarded as supplied except `Authorization`, which is rejected with an error. The core
owns authentication; accepting a caller-supplied bearer token would silently bypass the credential the
handle was created with.

---

## 7. Authentication

This is the section the whole package exists for, and it carries the most design weight.

### 7.1 Two access models

Entra ID offers two fundamentally different ways to reach Graph, and this package supports both. They are
normally **two separate app registrations**, because their permission sets, consent models and threat
profiles differ.

| | **Application-level access** | **Delegated access** |
|---|---|---|
| Acting as | The application itself | A signed-in user |
| Entra permission type | Application permissions | Delegated permissions |
| Consent | Admin consent, once, tenant-wide | User consent (or admin, tenant-wide) |
| Effective rights | Exactly the granted app permissions, across the whole tenant | Intersection of the granted scopes **and** what that user can already do |
| `/me` works | No — there is no user | Yes |
| Scope requested | `https://graph.microsoft.com/.default` | Explicit: `User.Read`, `Mail.Send`, … |
| Human needed | No | Yes, at first sign-in |
| Typical use | Daemons, scheduled jobs, tenant-wide automation | CLI tools, anything acting for a person |
| Client type | Confidential (holds a secret) | Public (holds no secret) |

The distinction that matters most in practice: **application permissions are not scoped to a user, so they
are tenant-wide.** `Mail.Read` as an application permission reads every mailbox in the tenant. The same
name as a delegated permission reads only the signed-in user's mail. Choosing the wrong model is how an
automation script ends up with far more reach than intended.

### 7.2 Entra app registration

The package cannot configure Entra for you; these are the settings it expects.

**Application-access app**

1. Register the app; note the *Directory (tenant) ID* and *Application (client) ID*.
2. **Certificates & secrets** → new client secret; record the value at creation (it is never shown again).
3. **API permissions** → Microsoft Graph → **Application permissions** → add what is needed.
4. **Grant admin consent.** Application permissions are inert without it.
5. No redirect URI, no public client flows.

**Delegated-access app**

1. Register the app; note the tenant and client IDs. **No client secret** — it is a public client.
2. **API permissions** → Microsoft Graph → **Delegated permissions** → add the scopes needed.
3. **Authentication** → *Allow public client flows* → **Yes**. Device code will not work without this.
4. **Authentication** → add a **Mobile and desktop applications** redirect URI matching what the auth-code
   flow will use, e.g. `http://localhost:8400`. Not needed for device code alone.
5. Admin consent only if a requested scope requires it; otherwise the user consents at first sign-in.

Both registrations feed the same Python package; only the `type` field in the credentials envelope differs.

### 7.3 Scopes

App-only uses `https://graph.microsoft.com/.default`, which resolves to exactly the application permissions
an administrator has consented to. The core requests nothing beyond that, and scope minimisation happens
where it belongs — in the app registration.

Delegated flows require **explicit scopes**, and the core refuses to default them. There is no safe default:
`.default` in a delegated flow silently requests every scope ever consented for that client, which is the
opposite of least privilege. Omitting `scopes` on a delegated credential envelope is an `invalidRequest`
error naming the missing field.

### 7.4 The strategy hierarchy

```csharp
internal abstract class AuthenticationStrategy
{
    /// Single-shot: credentials in, credential out. App-only flows only.
    public virtual Task<TokenCredential> CreateCredentialAsync(CancellationToken ct) =>
        throw new GraphCoreException("interactionRequired",
            $"'{GetType().Name}' requires the two-phase sign-in flow");

    /// Two-phase: returns a flow that must be begun and then completed.
    public virtual PendingAuthentication CreatePendingAuthentication() =>
        throw new GraphCoreException("interactionNotSupported",
            $"'{GetType().Name}' does not require interaction");
}
```

`ClientSecretStrategy` overrides the first; `DeviceCodeStrategy` and `AuthorizationCodeStrategy` override
the second.

> An earlier draft of this class also carried `AccessModel` and `RequiresInteraction`. Both were removed:
> nothing ever read them. The distinction they described is real and §7.1 documents it, but the base class
> already enforces it by throwing — a strategy that cannot do single-shot says so, and one that needs no
> interaction says so — which leaves the properties as labels nothing acted on. Adding certificate or managed-identity auth later means one new subclass overriding
`CreateCredentialAsync` and one line in the factory — nothing else in the codebase moves. That is the
concrete payoff of D11.

```csharp
internal static class AuthenticationStrategyFactory
{
    public static AuthenticationStrategy Create(CredentialsEnvelope c) => c.Type switch
    {
        "clientSecret"      => new ClientSecretStrategy(c),
        "deviceCode"        => new DeviceCodeStrategy(c),
        "authorizationCode" => new AuthorizationCodeStrategy(c),
        // "clientCertificate" => new ClientCertificateStrategy(c),
        // "managedIdentity"   => new ManagedIdentityStrategy(c),
        // "onBehalfOf"        => new OnBehalfOfStrategy(c),
        _ => throw new GraphCoreException("unsupportedCredentialType", $"unknown type '{c.Type}'")
    };
}
```

One factory, one `switch`, one reason to change. This is the single place in the core that knows the full
set of auth types.

### 7.5 Two-phase sign-in

Interactive flows must pause for a human, which a single synchronous ABI call cannot express. The
`PendingAuthentication` abstraction splits it:

```csharp
internal abstract class PendingAuthentication : IAsyncDisposable
{
    /// Returns what the user must see or do. Fast — no waiting.
    public abstract Task<BeginResult> BeginAsync(CancellationToken ct);

    /// Waits for the user to finish, then yields the credential.
    public abstract Task<TokenCredential> CompleteAsync(string inputJson, CancellationToken ct);
}
```

**Device code** — `BeginAsync` starts token acquisition on a background task and captures the device code
callback into a `TaskCompletionSource`, returning as soon as Microsoft issues the code. `CompleteAsync`
awaits that background task, which resolves when the user finishes signing in.

```text
Python                     Core                         Entra ID
  │ graph_auth_begin ────────►│                             │
  │                           │ ── start GetTokenAsync ────►│
  │                           │ ◄── device code + URL ──────│
  │ ◄── {code, url, expires}  │    (background task keeps polling)
  │                           │                             │
  │ print "visit … enter …"   │                       user signs in
  │                           │                             │
  │ graph_auth_complete ─────►│ ◄── token ──────────────────│
  │ ◄── {ok, handle}          │                             │
```

**Authorization code + PKCE** — `BeginAsync` generates the PKCE verifier and challenge, builds the
authorize URL, and returns the URL plus a `state` value. Python opens a browser and listens on the redirect
URI. `CompleteAsync` receives `{"code": …, "state": …}`, validates `state`, and exchanges the code.

```text
Python                     Core                         Entra ID
  │ graph_auth_begin ────────►│ generate verifier+challenge │
  │ ◄── {authorizeUrl, state} │ (verifier kept in the core) │
  │                           │                             │
  │ open browser ───────────────────────────────────────────►│
  │ ◄── redirect: code, state ───────────────────────────────│
  │ graph_auth_complete ─────►│ validate state              │
  │   {code, state}           │ ── code + verifier ────────►│
  │ ◄── {ok, handle}          │ ◄── token ──────────────────│
```

**The PKCE verifier never crosses the ABI.** It is credential material for the duration of the exchange;
keeping it inside the core means Python cannot leak it by logging a return value. `state` does cross, because
Python must compare it against what the redirect delivers — that is exactly what `state` is for.

**Pending flows are cleaned up.** `graph_auth_cancel` disposes one explicitly; the registry also evicts
flows past their expiry (device codes expire in about 15 minutes anyway), so an abandoned sign-in cannot
pin a background polling task forever.

### 7.6 Token lifetime and caching

Token acquisition, caching, expiry and refresh are entirely `Azure.Identity`'s responsibility. This package
writes no token-handling code: no manual `/oauth2/v2.0/token` calls, no expiry arithmetic, no refresh timer.
Every line not written here is a line that cannot mishandle a token.

Caching is **in-memory, for the life of the handle** (D6). Concretely:

- **App-only:** invisible. The credential re-acquires from the client secret whenever needed.
- **Delegated:** the user signs in once per `GraphClient`, and the credential refreshes silently while the
  handle lives. A new process means a new sign-in.

Nothing is written to disk, so there is no keyring dependency and no credential material at rest — the same
behaviour inside a container as outside. If repeated CLI invocations make re-prompting tiresome, §14 names
the upgrade.

### 7.7 Credential material

- Secrets are read from the environment **in Python**, passed once across the ABI, and live thereafter only
  inside the Azure.Identity credential.
- Never logged, never echoed into an envelope, never written to disk by this package.
- **No export returns an access token or a refresh token.** A raw-token escape hatch would be a deliberate,
  separately reviewed addition — the current surface does not permit one.
- Header filtering (§6.7) is the mechanical guarantee that credential material cannot leak back through the
  return path.

> **Known ceiling.** `ClientSecretCredential` takes a `string`, and a managed `string` cannot be reliably
> zeroed — the GC may copy it, and it persists on the heap until collected. A process memory dump can
> therefore contain the secret. Mitigating this properly means certificate auth or managed identity, which
> remove the shared secret entirely. Noted so the limitation is known rather than assumed away.
>
> Delegated flows are better off here by construction: a public client holds no secret at all.

---

## 8. Request handling

### 8.1 The handler pipeline

`GraphSession` builds its `HttpClient` from Microsoft's factory, identically for both access models — once
a `TokenCredential` exists, nothing downstream knows or cares how it was obtained:

```csharp
var handlers = GraphClientFactory.CreateDefaultHandlers();
handlers.Add(new AuthorizationHandler(authProvider));
var http = GraphClientFactory.Create(handlers);
```

That pipeline supplies, as supported Microsoft code:

| Handler | Responsibility |
|---|---|
| `RetryHandler` | Retries `429`, `503`, `504`; **honours `Retry-After`**; exponential backoff; configurable ceiling via `RetryHandlerOption` |
| `RedirectHandler` | Follows Graph redirects, notably pre-authenticated download URLs |
| `UserAgentHandler` | Identifies the SDK to Graph telemetry |
| `ParametersNameDecodingHandler` | Correct OData parameter encoding |
| `AuthorizationHandler` | Attaches the bearer token; retries once on a **CAE claims challenge** — a bare `401` is reported, not retried |

> **Verified against Microsoft.Graph.Core 4.0.1.** `CreateDefaultHandlers()` contains no
> `CompressionHandler`; an earlier draft of this table listed one. The full list is
> `UriReplacement`, `Retry`, `Redirect`, `ParametersNameDecoding`, `UserAgent`,
> `HeadersInspection`, `BodyInspection` and `GraphTelemetry`.
>
> It also emerged that `RetryHandler` **throws** `AggregateException`/`ApiException` once its
> budget is exhausted rather than returning the last response. `ErrorEnvelope.From` rebuilds a
> faithful envelope from the exception; without that, a throttled call degraded to
> `internalError` with no status and no `Retry-After`, breaking the promise two paragraphs below.

**No retry, backoff or throttling logic is written in this package.** CLAUDE.md's "avoid implementing custom
infrastructure when the Microsoft Graph SDK or .NET already provides the required behavior" resolves this
completely: the retry semantics are Microsoft's, tested by Microsoft, and correct on the cases a hand-rolled
loop gets wrong. Per-session tuning is exposed as optional `retry` fields on the creds envelope
(`maxRetries`, `maxDelaySeconds`), mapping onto `RetryHandlerOption`.

Throttling is handled twice over: transparently by `RetryHandler`, and — for cases that exhaust the retry
budget — visibly, via `retryAfterSeconds` in the error envelope so the Python caller can back off itself.

### 8.2 The operation hierarchy

Every request type shares a pipeline: build a URL, apply headers, send, map the response, normalise errors.
`GraphOperation` holds that as a template method; subclasses fill in what differs.

```csharp
internal abstract class GraphOperation
{
    public async Task<ResponseEnvelope> ExecuteAsync(GraphSession session, CancellationToken ct)
    {
        var request = BuildRequest(session.UrlBuilder);      // subclass
        ApplyHeaders(request);                               // shared, with the allowlist
        using var response = await SendAsync(session, request, ct);   // subclass may override
        return response.IsSuccessStatusCode
            ? await MapSuccessAsync(response, ct)            // subclass
            : await MapErrorAsync(response, ct);             // shared — one error shape, always
    }
    // …
}
```

`JsonRequestOperation`, `DownloadOperation`, `UploadOperation` and `BatchOperation` differ only in
`BuildRequest` and `MapSuccessAsync`. Error mapping is inherited, which is what guarantees a download
failure and a batch failure produce the same envelope shape as an ordinary request failure — the
consistency §9 promises is structural, not a convention someone has to remember.

### 8.3 URL construction

```text
absolute path?  ──yes──►  use verbatim, ignore version and query
      │no
      ▼
https://graph.microsoft.com/{version}{path}?{urlencoded query}
```

Absolute-URL passthrough is what makes pagination, pre-authenticated download URLs and `Location`-header
follow-ups work without special cases.

> **Platform trap, found by the first Linux build.** "Is this absolute?" must **not** be decided with
> `Uri.TryCreate(path, UriKind.Absolute, …)`. On Unix that returns `true` for `/users`, parsing it as the
> file URI `file:///users`; on Windows it returns `false`. Deciding on it rejected every relative Graph
> path on Linux — the only platform this ships to — while the suite stayed green on the development
> machine. The discriminator is the scheme separator, which no Graph path contains and every URL does.

### 8.4 Pagination

The core does not iterate. It lifts `@odata.nextLink` into the envelope, and Python owns the loop:

```python
def paged(self, path, **kw):
    resp = self.request("GET", path, **kw)
    while True:
        yield from resp["body"].get("value", [])
        nxt = resp.get("nextLink")
        if not nxt:
            return
        resp = self.request("GET", nxt)
```

No handles, no cursor state in the native library, nothing to leak if a Python generator is abandoned
half-way. A Python generator is the language-native equivalent of the `IAsyncEnumerable<T>` CLAUDE.md asks
for, and the C# side still uses `IAsyncEnumerable<T>` internally where it reads naturally — for example,
when `BatchOperation` walks paged sub-responses.

`@odata.count` and `@odata.deltaLink` remain in `body` untouched, so `$count` and delta queries work with no
additional support.

### 8.5 Batching

`POST /$batch` is otherwise an ordinary request, but two pieces of behaviour belong to the core:

1. **Chunking at 20.** Graph rejects batches larger than 20 requests. `BatchOperation` splits a larger set,
   issues the chunks, and merges the responses.
2. **Re-ordering by `id`.** Graph does not guarantee response order within a batch. The operation restores
   input order, so results align positionally with what was sent.

This lives in C# rather than Python even though Python is the easier place to write it, because it is Graph
behaviour, and CLAUDE.md makes C# the source of truth for Graph behaviour.

Per-request failures inside a batch are *not* raised as exceptions: a batch returns a list of per-request
envelopes, each with its own `ok`/`status`/`error`. One failing sub-request must not discard nineteen
successful ones.

> **Known ceiling.** Chunks are issued sequentially, and `dependsOn` chains are passed through but not
> validated across a chunk boundary — a dependency spanning two chunks will fail at Graph. Both are
> acceptable at expected volumes; the fix is concurrent dispatch and dependency-aware partitioning, worth
> adding only if batch latency becomes a measured problem.

### 8.6 Downloads

`graph_download` takes `{"path": …, "destPath": …}` and streams with
`HttpCompletionOption.ResponseHeadersRead` directly into a `FileStream`. Response bytes never enter a JSON
envelope and never fully enter memory, so file size is bounded by disk rather than RAM.

Returns `{"ok": true, "status": 200, "bytesWritten": 1048576, "destPath": "…"}`.

The `destPath` directory must exist and be writable; the core does not create directories. It writes to a
temporary sibling file and renames on success, so a failed download cannot leave a truncated file at the
destination path.

### 8.7 Uploads

`UploadOperation` selects an `IUploadStrategy` by file size — the Strategy pattern doing exactly what it is
for, since the two paths share nothing but their input:

| Size | Strategy | Mechanism |
|---|---|---|
| < 4 MiB | `SimpleUploadStrategy` | Single `PUT` of the file stream |
| ≥ 4 MiB | `ChunkedUploadStrategy` | `createUploadSession`, then sequential `PUT`s with `Content-Range` |

Chunk size is 10 MiB and **must be a multiple of 320 KiB**, which Graph requires for upload sessions.
Transient chunk failures are retried against the session's `nextExpectedRanges`, which is what makes a large
upload resumable within a single call.

> **Resolved at milestone 5.** `LargeFileUploadTask` cannot be used at all. Beyond being generic over the
> `IParsable` result types D2 excludes, its constructor requires a Kiota `IRequestAdapter`, which this package
> deliberately never builds. `ChunkedUploadStrategy` is therefore the self-contained chunk loop. As predicted,
> the choice stayed behind `IUploadStrategy` and invisible to every other class.

### 8.8 Cancellation and timeouts

Every request carries `timeoutMs` (default 100 s). The export creates a `CancellationTokenSource` from it and
propagates the token through every async call down to `HttpClient` and the file streams.

> **Known ceiling.** There is no `graph_cancel` export for in-flight *requests* in v1, so a `Ctrl-C` during a
> blocking `ctypes` call is not delivered until the call returns. Pending *sign-ins* are cancellable, via
> `graph_auth_cancel`. The upgrade path is a `graph_cancel(int64 requestId)` export over a registry of live
> `CancellationTokenSource`s — worth adding the first time someone needs to abort a multi-gigabyte upload.

### 8.9 Concurrency

A handle is safe to share across Python threads: `HttpClient` is thread-safe, Azure.Identity credentials
serialise token refresh internally, and `HandleRegistry<T>` is backed by a `ConcurrentDictionary`. Two threads
issuing requests on one handle share a connection pool and a token cache, which is the desirable outcome.

Creating a handle per thread is supported but wasteful — and for delegated access it is worse than wasteful,
because each handle would prompt its own sign-in.

---

## 9. Error model

### 9.1 Shape

Every failure — transport, auth, Graph, or a bug in the core — produces the same envelope:

```json
{
  "ok": false,
  "status": 429,
  "error": {
    "code": "activityLimitReached",
    "message": "Too many requests. Please retry after some time.",
    "requestId": "a1b2c3d4-…",
    "clientRequestId": "e5f6a7b8-…",
    "date": "2026-09-22T10:14:32Z",
    "retryAfterSeconds": 12,
    "innerError": { "code": "quotaLimitReached", … }
  }
}
```

Fields absent from the Graph response are omitted rather than nulled. `innerError` is preserved verbatim —
CLAUDE.md asks that useful information from Graph responses be preserved, and Graph's inner errors are
frequently the only actionable part of a failure.

### 9.2 Non-HTTP failures

Failures with no HTTP response use `status: 0` and a core-defined `code`, so Python has exactly one error
shape to handle:

| `code` | Cause |
|---|---|
| `authenticationFailed` | `AuthenticationFailedException` — bad secret, wrong tenant, expired credential |
| `consentRequired` | A requested delegated scope has not been consented to |
| `interactionRequired` | A two-phase flow was used through `graph_client_create` |
| `signInTimeout` | The user did not complete sign-in within the window |
| `signInDeclined` | The user cancelled, or Entra denied the sign-in |
| `stateMismatch` | The auth-code redirect `state` did not match — a possible CSRF attempt |
| `transportError` | DNS, TLS, connection reset |
| `timeout` | `timeoutMs` elapsed |
| `invalidRequest` | Malformed envelope, or a delegated credential missing `scopes` |
| `invalidHandle` | Handle unknown or already closed |
| `unsupportedCredentialType` | Unknown `type` in the creds envelope |
| `internalError` | A bug in the core — the catch-all from §6.2 |

`authenticationFailed` messages are passed through from Azure.Identity, which is careful not to include
secret material. The core adds no credential detail of its own.

### 9.3 Python surface

`_native.py` raises when `ok` is `false`:

```python
class GraphError(Exception):
    status: int            # HTTP status, or 0 for non-HTTP failures
    code: str              # Graph error code, or a core code from the table above
    message: str
    request_id: str | None # quote this to Microsoft support
    retry_after: int | None
    inner: dict | None
```

One exception type, not a hierarchy: callers branch on `status` and `code`, which is more precise than any
class tree and does not require importing eight names to write an `except` clause. This is a deliberate
exception to §4's OOP guidance — Python's idiom here is a flat exception carrying data, and the variation is
in values, not behaviour.

---

## 10. NativeAOT constraints

NativeAOT is what makes the wheel dependency-free, and it is also the highest-risk part of this design.
Three constraints follow from it.

**Reflection-free JSON.** The project sets:

```xml
<PublishAot>true</PublishAot>
<NativeLib>Shared</NativeLib>
<InvariantGlobalization>true</InvariantGlobalization>
<JsonSerializerIsReflectionEnabledByDefault>false</JsonSerializerIsReflectionEnabledByDefault>
```

The last line turns any accidental reflection-based serialisation into a build-time failure instead of a
runtime one. Envelope DTOs are handled by a source-generated context:

```csharp
[JsonSerializable(typeof(RequestEnvelope))]
[JsonSerializable(typeof(ResponseEnvelope))]
[JsonSerializable(typeof(ErrorEnvelope))]
[JsonSerializable(typeof(CredentialsEnvelope))]
[JsonSerializable(typeof(BeginResultEnvelope))]
internal partial class GraphJsonContext : JsonSerializerContext;
```

Arbitrary Graph response bodies are never deserialised into types — they stay as `JsonNode`, which is
reflection-free by construction. This is also why D2 matters: the typed SDK would mean thousands of generated
types to keep AOT-clean, for no benefit once everything becomes JSON anyway.

**Which auth flows survive AOT.** This is what constrains D4:

| Flow | AOT | Status |
|---|---|---|
| Client secret | ✅ | v1 |
| Device code | ✅ | v1 |
| Authorization code + PKCE | ✅ | v1 |
| Client certificate | ✅ | extension point ready |
| Managed identity | ✅ | extension point ready |
| On-behalf-of | ✅ | extension point ready |
| Interactive browser | ❌ | excluded — reflection and platform UI |
| WAM / broker | ❌ | excluded — native broker interop |

The two excluded flows are precisely why auth-code+PKCE is structured with Python owning the browser (§11.3):
it delivers the interactive-browser experience while the core does only the AOT-safe half — the code-for-token
exchange.

> **Resolved at milestone 3.** Azure.Identity's `AuthorizationCodeCredential` is unusable here on two
> counts: it exposes no PKCE code verifier, and *every* constructor demands a `clientSecret` that the public
> client of §7.2 does not have. The MSAL.NET fallback is what ships —
> `AcquireTokenByAuthorizationCode(…).WithPkceCodeVerifier(…)` against a secretless confidential client,
> wrapped in `MsalAuthorizationCodeCredential`. `Microsoft.Identity.Client` becomes a direct reference but
> was already transitive via Azure.Identity, so the dependency graph is unchanged. As predicted, the
> abstraction confined it: nothing downstream of the credential knows.
>
> **Still unproven:** that Entra accepts a secretless PKCE exchange for a given app registration. MSAL builds
> the client; only a live tenant can show whether the service accepts the request.

**Diagnostics are thinner.** Stack traces in an AOT binary are less complete than under the JIT. The core
compensates by attaching `code` and `requestId` to every error and keeping the call stack shallow —
export → executor → operation → pipeline.

> **Settled.** The `Azure.Identity` + Kiota handler stack is AOT-clean. `dotnet publish -r linux-x64`
> generates native code in roughly twenty seconds with **no trim or AOT warnings**, producing an 11.5 MB
> stripped shared library that exports exactly the nine documented entry points and nothing else. D1, D2 and
> this section are no longer a risk.

---

## 11. The Python layer

### 11.1 Dependencies

`ctypes`, `json`, `secrets`, `webbrowser` and `http.server` — all stdlib. Nothing else. A package whose
selling point is "plug and play" should not begin with a dependency resolution.

### 11.2 Surface

```python
from msgraph_simple import GraphClient, GraphError

# ── Application-level access ────────────────────────────────────────────
g = GraphClient.app_only(tenant_id=..., client_id=..., client_secret=...)
g = GraphClient.from_env()                    # AZURE_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET

# ── Delegated access: device code ───────────────────────────────────────
with GraphClient.device_code(tenant_id=..., client_id=...,
                             scopes=["User.Read"]) as g:
    ...                                       # prints the code+URL, then blocks for sign-in

flow = GraphClient.begin_device_code(...)     # two-phase, for custom prompting
print(flow.user_code, flow.verification_uri, flow.expires_in)
g = flow.complete()

# ── Delegated access: authorization code + PKCE ─────────────────────────
with GraphClient.interactive(tenant_id=..., client_id=...,
                             scopes=["User.Read"],
                             redirect_uri="http://localhost:8400") as g:
    ...                                       # opens a browser, catches the redirect

# ── Requests: identical for every access model ──────────────────────────
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

Once a `GraphClient` exists, **nothing about the request surface depends on how it was authenticated.** That
is the encapsulation the strategy hierarchy buys, surfacing in the Python API: scripts can be written against
`GraphClient` and switched between access models by changing one constructor call.

Conveniences worth their few lines: OData parameters as Python keywords (`select=`, `filter=`, `top=`,
`expand=`, `orderby=`) mapping onto `$select` and friends; `get`/`post`/`patch`/`delete` as thin wrappers over
`request`; context-manager support so `graph_client_close` always runs; `from_env()` for the environment
variable path that motivated the package.

Everything else is passthrough. The Python layer contains **no Graph knowledge** — no endpoint lists, no retry
logic, no error interpretation beyond raising what the core reports.

### 11.3 The redirect listener

`interactive()` is the one place Python does real work, because the core cannot open a browser:

1. Call `graph_auth_begin` → receive `authorizeUrl` and `state`.
2. Start a single-request `http.server` on the redirect URI's port.
3. `webbrowser.open(authorize_url)`.
4. Wait for the redirect; pull `code` and `state` from the query string; serve a small "you may close this
   window" page.
5. Call `graph_auth_complete` with `{"code": …, "state": …}` → receive the session handle.

Python never sees the PKCE verifier and never touches a token — it shuttles an authorization code, which is
single-use, short-lived and worthless without the verifier the core kept. The listener binds to `127.0.0.1`
only, accepts exactly one request, and times out with the sign-in window. `state` is validated **in the core**,
not in Python, so the CSRF check cannot be skipped by a caller reimplementing this loop.

### 11.4 Library loading

`_native.py` resolves `MicrosoftGraph.so` from the package's `_lib/` directory, declares `argtypes` and
`restype` for all nine exports (omitting these is the classic way to corrupt pointers on 64-bit platforms),
verifies `coreVersion`, and exposes only `_call`.

`restype` for the eight envelope-returning functions is `ctypes.c_void_p`, not `c_char_p`: `c_char_p` makes
`ctypes` auto-convert to `bytes` and discard the pointer, which would make `graph_free` impossible and leak
every response.

### 11.5 Type hints

`py.typed` ships with the package and the public surface is fully annotated. An editor should complete
`g.paged(` without the user opening the docs.

---

## 12. Build and packaging

### 12.1 The cross-compilation consequence

**NativeAOT cannot cross-compile from Windows to Linux.** It invokes the platform linker and links against
the platform's native libraries, so a `linux-x64` binary must be produced on Linux.

The development machine is Windows 11 with .NET 10.0.401 and Python 3.14.6. That machine cannot load the
resulting `.so`. Two direct consequences:

1. **The build runs in Docker.** `build/Dockerfile` is the definition of a build; there is no "works on my
   machine" path.
2. **The Python development loop lives in Docker or WSL.** Editing happens on Windows; running and testing the
   Python package happens in Linux.

C# unit tests are exempt — they are plain managed code and run natively on Windows with `dotnet test`. Only
the AOT binary and anything loading it require Linux. That keeps the fast inner loop (write C#, run xUnit) on
the host and pushes only the slower outer loop (build `.so`, run Python) into a container.

Adding `win-x64` for a native Windows development loop is a one-line RID change plus a second build leg — a
packaging change, not a redesign. It is deliberately deferred (D12) rather than designed out.

### 12.2 Build

```dockerfile
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
RUN apt-get update && apt-get install -y clang zlib1g-dev   # NativeAOT linker prerequisites
WORKDIR /src
COPY . .
RUN dotnet publish src/MicrosoftGraph -r linux-x64 -c Release -o /out
```

```bash
docker build -f build/Dockerfile -t msgraph-core-build .
docker run --rm -v "$PWD/python/msgraph_simple/_lib:/dest" msgraph-core-build \
       cp /out/MicrosoftGraph.so /dest/
```

### 12.3 Wheel

The wheel is platform-specific and contains a compiled binary, so it is tagged accordingly rather than as
`py3-none-any`. Building inside the .NET SDK image (Debian-based) sets the glibc floor, and the wheel is
tagged to match. The floor is read from the library itself rather than assumed: the highest versioned
glibc symbol it imports is `GLIBC_2.34`, so the artefact is
`msgraph_simple-0.1.0-py3-none-manylinux_2_34_x86_64.whl`.

Neither half of that tag is what setuptools produces unaided. From `pyproject.toml` alone it emits
`py3-none-any`, which installs cheerfully on Windows and then fails at import; declaring the distribution
impure over-corrects to `cp314-cp314`, pinning one interpreter. `python/setup.py` overrides `bdist_wheel`
to get both halves right at once.

It is `py3-none-*` rather than `cp314-*`: `ctypes` is ABI-stable across CPython versions, so one wheel serves
every supported Python 3. That is a genuine advantage of `ctypes` over a C extension.

### 12.4 Commands

```bash
dotnet build                        # host build, fast feedback
dotnet test                         # C# unit + integration tests, Windows or Linux
dotnet format                       # style
docker build -f build/Dockerfile -t msgraph-core-build .        # linux-x64 library
docker run --rm -v "$PWD/python/msgraph_simple/_lib:/dest"        msgraph-core-build cp /out/MicrosoftGraph.so /dest/      # bundle it into the package

python -m unittest discover -s python/tests                     # needs the library above
python -m build --wheel python/        -C--build-option=--plat-name=manylinux_2_34_x86_64       # wheel, run inside Linux
```

There is one artefact: the wheel. The managed assembly is a build intermediate, not something anyone
references — see §2.

---

## 13. Testing strategy

xUnit and FluentAssertions, per CLAUDE.md, in four layers. The class design of §4 is what makes the first two
possible: every strategy and operation is constructible in isolation with a stub `HttpMessageHandler`, so
almost nothing requires the ABI or the network.

**Unit tests** — `tests/UnitTests`, stubbed transport, no network:

- *URL building:* relative path + version + query; absolute-URL passthrough ignoring version and query; OData
  parameter encoding.
- *Headers:* request forwarding; rejection of caller-supplied `Authorization`; response allowlisting — assert
  `Authorization` and `WWW-Authenticate` never appear in output.
- *Errors:* Graph error JSON → error envelope, including `innerError` preservation and `Retry-After` parsing;
  every `GraphOperation` subclass produces the same error shape.
- *Pagination:* `@odata.nextLink` lifted to `nextLink`; absent on the last page.
- *Batch:* chunking at 20; re-ordering by `id`; a failing sub-request does not fail its siblings.
- *Auth — factory:* each `type` yields the right strategy; unknown types raise `unsupportedCredentialType`.
- *Auth — access models:* app-only defaults to `.default`; **a delegated envelope without `scopes` is
  rejected** (§7.3); a two-phase strategy through `graph_client_create` raises `interactionRequired`.
- *Auth — PKCE:* verifier meets RFC 7636 length and charset; challenge is the correct S256 transform;
  **the verifier never appears in any envelope**; `state` mismatch raises `stateMismatch`.
- *Auth — device code:* `BeginAsync` returns code, URI and expiry without blocking; expiry cancels the pending
  flow and disposes the background task.
- *Upload strategy selection:* 4 MiB is the boundary; chunk sizes are multiples of 320 KiB.
- *Serialisation:* every envelope round-trips through the source-generated context.

**Integration tests** — `tests/IntegrationTests`, the full handler pipeline with a controlled transport and a
fake `TokenCredential`:

- A `429` carrying `Retry-After: 2` is retried and the delay honoured; budget exhaustion surfaces
  `retryAfterSeconds` rather than looping.
- A `401` triggers exactly one token re-acquisition and one retry.
- Download streams to disk without buffering; a mid-transfer failure leaves no file at `destPath`.
- An upload above 4 MiB creates a session and sends correctly-sized, correctly-ranged chunks.
- A full two-phase device code flow against a stubbed Entra endpoint: begin → code issued → complete → handle.
- **No token, secret, PKCE verifier or `Authorization` header appears in any envelope, log line or exception
  message** — asserted across every scenario above, not tested once in isolation.

**ABI tests** — the layer most likely to fail catastrophically and least likely to fail visibly. Run against
the built `.so` from Python:

- `create → request → free → close` round-trips well-formed envelopes.
- `auth_begin → auth_complete → request → close`, and `auth_begin → auth_cancel`, both clean up.
- Malformed JSON, a null pointer, an unknown handle, a closed handle and an unknown flow id each return an
  **error envelope** rather than crashing the process. This is the test for §6.2 and the most important test in
  the suite: its failure mode is a dead interpreter, not a red assertion.
- A repeated create/request/close loop shows no growth in RSS — the check that `graph_free` is wired correctly.
- Abandoned pending flows are evicted at expiry and leave no live background task.
- `coreVersion` mismatch raises at load time.

**Live tests** — real Graph, real tenants, skipped unless the relevant environment variables are present, so
`dotnet test` on a clean checkout never requires a tenant. Two sets, because there are two app registrations
(§7.2): an app-only suite that runs unattended in CI, and a delegated suite that requires a human and is
therefore run manually before a release.

---

## 14. Deliberate omissions

Each is a decision, not an oversight. Each has a trigger that should bring it in.

| Omitted | Why | Add when |
|---|---|---|
| Persistent token cache | No credential material at rest, no keyring dependency, identical in a container (D6) | Repeated CLI invocations make re-prompting tiresome enough to accept a refresh token on disk |
| Interactive browser / WAM auth | Not NativeAOT-compatible (§10) | Never, under D1 — auth-code+PKCE (§11.3) delivers the same UX within AOT limits |
| Certificate / managed-identity / OBO auth | Three flows the extension point is already shaped for (§7.4) | Deploying to Azure (managed identity), or moving off shared secrets (certificate) |
| ROPC (username/password) | Breaks under MFA and Conditional Access; stores user passwords | Effectively never; device code covers the headless case properly |
| `graph_cancel` for in-flight requests | `timeoutMs` covers the common case; sign-ins are already cancellable | Someone needs to abort a long upload from outside |
| Typed Graph models | Generic JSON reaches every `v1.0` and `beta` endpoint on day one | Never, under the revised §2 — anyone wanting typed request builders in .NET should take `Microsoft.Graph` itself rather than this |
| Native async | `ctypes` releases the GIL, so `asyncio.to_thread` covers it | Measured thread-pool pressure from high request concurrency |
| Response caching | Graph's `ETag`/`If-None-Match` support is passthrough already | A measured hot path re-fetches unchanged data |
| ~~Structured logging~~ | **Added.** `Diagnostics/GraphLog`: opt-in via `MSGRAPH_LOG_LEVEL`, one JSON object per line on stderr, off by default. Logs `requestId`, never headers, and never the query string — an OData `$filter` carries user identifiers. No dependency, no DI container, no ABI change | — |
| `win-x64` / `osx-arm64` builds | `linux-x64` is the deployment target (D12) | A native Windows dev loop is wanted, or macOS deployment appears |
| Concurrent batch chunk dispatch | Sequential is correct and simpler | Batch latency is measured as a problem |

---

## 15. Build order

Each milestone has a verification that either passes or does not. Do not start one before its predecessor's
check is green.

> **All seven are green.** Built and verified on Ubuntu 26.04 under WSL with .NET 10.0.401 and clang 21.
> 171 managed tests pass on Windows and Linux alike; 61 Python tests pass against the real compiled core,
> with one skip — the live-tenant suite. What remains unproven is only what needs a tenant: see the note at
> the end of this section.

1. **AOT spike.** `MicrosoftGraph.csproj` plus one export that builds a `ClientSecretCredential`, issues
   `GET /users`, and returns the JSON. Build in Docker.
   → **Verify:** a Python `ctypes` script loads the `.so` and prints a real user from a real tenant.
   → **Done.** Native code generated in ~20s with no trim or AOT warnings. An 11.5 MB stripped library
   exporting exactly the nine entry points. D1, D2 and §10 are settled.

2. **Core request path, application access.** Models, `GraphJsonContext`, `HandleRegistry<T>`, `GraphSession`,
   `GraphOperation` + `JsonRequestOperation`, `AuthenticationStrategy` + `ClientSecretStrategy`, the factory,
   and the four exports `client_create` / `client_close` / `request` / `free`.
   → **Done.** Unit and integration suites green; the `Authorization` assertions pass.

3. **Delegated access.** `PendingAuthentication`, `DeviceCodeStrategy`, `AuthorizationCodeStrategy` with PKCE,
   and the three exports `auth_begin` / `auth_complete` / `auth_cancel`. Resolves the PKCE verification item
   in §10.
   → **Done.** PKCE and scope-required tests pass; a stubbed end-to-end device code flow yields a session
   that calls Graph. The §10 verification item resolved against expectation — see there.

4. **Python package.** `_native.py`, `_auth.py`, `GraphClient` with `app_only` / `from_env` / `device_code` /
   `interactive`, `paged`, `GraphError`, `py.typed`.
   → **Done, except the live half.** All 19 ABI tests pass against the compiled core: the no-crash cases,
   the RSS-stability loops, handle lifecycle and version agreement. A real device code sign-in still needs a
   tenant.

5. **Files.** `DownloadOperation`, `UploadOperation`, `IUploadStrategy` and both implementations. Resolves the
   `LargeFileUploadTask` verification item in §8.7.
   → **Done.** A file over 4 MiB round-trips through an upload session with matching SHA-256; a failed
   download leaves nothing at `destPath`.

6. **Batching.** `BatchOperation` — chunk at 20, merge, restore input order.
   → **Done.** A 25-request batch returns 25 responses in submission order, with a deliberately failing
   sub-request reported in place rather than raised.

7. **Packaging.** `build/Dockerfile`, wheel build, CI on the Linux leg.
   → **Done, except the live half.** The container build succeeds, runs the managed suite inside the image,
   and yields the library. The wheel installs into a clean virtual environment, imports, loads and
   version-checks the core, creates a session offline, and returns clean error envelopes for a closed handle,
   malformed JSON and an unknown handle — with the interpreter still alive, which is the point. A smoke test
   against a live tenant still needs a tenant.

---

### What a tenant would still settle

Everything above is verified without one. These are not:

- Whether Entra accepts a **secretless PKCE exchange** for a given app registration (§10). MSAL builds the
  request; only the service can accept it. This is the largest remaining unknown.
- A real device code sign-in end to end, and the silent refresh that follows it.
- That the permissions in §7.2 are sufficient in practice for each sample.

---

## Appendix: the guiding principle, applied

CLAUDE.md asks for *"the smallest reliable C# core that provides unified Microsoft Entra ID authentication and
Microsoft Graph request handling."* The project also asks for object-oriented design that pays off in
reusability. §4 states how those reconcile; this is what the reconciliation produced.

**Small:** nine exports, three NuGet dependencies, zero Python dependencies, zero lines of hand-written retry or
token logic. Pagination has no server state. Binary payloads use the filesystem instead of a second protocol.

**Object-oriented where it pays:** five abstractions, each with at least two implementations on day one.
The test of whether they earn their place is the diff for a change they were built to absorb — adding
certificate authentication is one new class and one `case`; swapping the chunked-upload implementation touches
one file; adding an operation type touches no existing class. Neither the ABI, the Python layer, nor any
consumer script changes in any of those cases.

**Concrete where abstraction would be ceremony:** no interface with one implementation, no DI container, no
repository layer, no `Manager` or `Helper` in any name.

The places where restraint was **not** applied are deliberate and all sit on trust boundaries: the blanket
`catch` at every export (§6.2), the response header allowlist (§6.7), the refusal to default delegated scopes
(§7.3), keeping the PKCE verifier inside the core (§7.5), the no-token-export rule (§7.7), and the ABI crash
tests (§13). Simplicity is the default everywhere else; it is never a reason to fail open.
