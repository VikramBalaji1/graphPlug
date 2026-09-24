# CLAUDE.md

## Overview

A plug-and-play Python package for Microsoft Entra ID authentication and Microsoft Graph, with a
resource layer that makes mail, calendar, files, teams and users one call each rather than twenty
lines of nested JSON.

Pure Python, at the repository root. An earlier C# implementation has been removed.
ARCHITECTURE.md documents the current design and why each rule is what it is.

## Tech Stack

* Python 3.9+, async throughout
* azure-identity (credentials)
* msgraph-core (the supported middleware pipeline)
* unittest with IsolatedAsyncioTestCase, and httpx.MockTransport as the seam

Deliberately NOT msgraph-sdk: its dependency tree does not resolve in practice.

## Project Structure

```text
msgraph_simple/
├── __init__.py       public surface: GraphClient, GraphError, PendingSignIn, Scopes
├── _http.py          the middleware pipeline, auth attachment, concurrency limit
├── _auth.py          credentials, PKCE, the loopback redirect listener
├── _request.py       URL building, OData, the response header allowlist
├── _errors.py        GraphError and the code taxonomy
├── _operations.py    paging, batching, download, the two upload strategies
├── _log.py           opt-in structured logging
├── _scopes.py        named permissions
└── _resources/       base.py, mail.py, calendar.py, files.py, teams.py, users.py

tests/     the suite
samples/   runnable scripts
docs/      troubleshooting
```

Keep `msgraph_simple/` limited to the production code required by the package.

Add files and folders only when the implementation requires them.

### `_auth.py`

Microsoft Entra ID credential and sign-in handling. Use `azure-identity`; no token acquisition,
caching, expiry or refresh is written here.

Support applicable authentication scenarios including:

* Delegated authentication
* Client credentials
* Managed identity
* On-behalf-of authentication

Any `azure.core.credentials_async.AsyncTokenCredential` is accepted, so the scenarios this package
does not construct itself are still a credential swap away.

Keep credential handling centralized.

### `_http.py`, `_request.py`, `_operations.py`

Microsoft Graph request construction and execution.

Use `msgraph-core`'s middleware for the HTTP infrastructure Microsoft supports. Paging, batching
and chunked upload are this package's own, because Kiota's versions require a `RequestAdapter`
this package does not use.

Support:

* Graph API `v1.0`
* Graph API `beta` where required
* Generic Graph requests
* OData queries
* Pagination
* Batch requests
* File uploads and downloads
* Streaming
* Custom Graph headers
* Request cancellation
* Retry and throttling

Use an async generator where it provides a natural interface for paginated operations.

### `_resources/`

Mail, calendar, files, teams, users, and the shared base they derive from. A resource declares a
path and its scopes and adds only what is specific to it. Add a resource when there is a caller for
it, not before.

## Authentication

Centralize authentication through Microsoft's credential abstractions.

Use secure credential and configuration providers.

Never expose or log:

* Access tokens
* Refresh tokens
* Client secrets
* Authorization headers
* Other credential material

Request only the Microsoft Graph permissions required by the functionality.

## Microsoft Graph

Use `msgraph-core` and the rest of Microsoft's supporting libraries where they provide the required functionality.

Use `v1.0` by default.

Support `beta` explicitly when functionality is only available there.

Provide generic request handling so Graph endpoints can be accessed without requiring a dedicated implementation for every endpoint.

## Requests

Support the HTTP methods and request patterns required by Microsoft Graph.

Every asynchronous operation accepts and propagates:

```python
cancellation via the caller's own asyncio task, and an explicit `timeout` where Graph needs one
```

Handle Graph:

* Request construction
* Responses
* Pagination
* Batch requests
* File and stream operations
* OData parameters
* Custom headers
* Errors
* Retries
* Throttling

Respect `Retry-After` when provided by Microsoft Graph.

## Errors

Provide consistent Graph error information including, where available:

* HTTP status
* Graph error code
* Error message
* Request identifiers
* Retry information

Preserve useful information from Microsoft Graph responses.

## Reliability

Use Microsoft's supported retry infrastructure where appropriate.

Handle transient failures and Graph throttling consistently.

Avoid implementing custom infrastructure when azure-identity or msgraph-core already provides the required behavior.

## Testing

Use `unittest` with `IsolatedAsyncioTestCase`. The seam is `httpx.MockTransport`, wrapped by the
real msgraph-core middleware, so tests exercise the same retry and redirect path production does.
No test dependency beyond the package's own.

Tests cover:

* Authentication behavior
* Request construction
* Graph responses
* Pagination
* Error handling
* Retry behaviour, including that the middleware pipeline actually engages
* Batching, upload strategy selection and file round-trips
* The exact paths and payloads every resource builds
* Concurrency bounds

Live Graph tests use dedicated test environments and credentials, and skip without them.

## Dependencies

Keep dependencies minimal.

Prefer:

* The Python standard library
* azure-identity
* msgraph-core

Use additional dependencies only when they provide functionality required by the package.

## Package

The Python wheel is the only package artifact, and it is pure Python: `py3-none-any`, installable
anywhere.

Behaviour is defined by Microsoft's own libraries wherever they provide it. Where they do not --
batching, paging, chunked upload, the resource payloads -- this package owns it, and
ARCHITECTURE.md records why each rule is what it is.

## Development

Test:

```bash
python -m unittest discover -s tests
```

Install for development:

```bash
pip install -e .
```

Build the wheel:

```bash
python -m build --wheel
```

## Guiding Principle

Build the smallest reliable Python package that makes Microsoft Graph plug and play: one call to
send a mail, one to book a meeting, and nothing to configure.

