# CLAUDE.md

## Overview

A plug-and-play Python package for Microsoft Entra ID authentication and Microsoft Graph, with a
resource layer that makes mail and calendar work one call rather than twenty lines of nested JSON.

Python is the implementation. The C# tree under `src/` and `tests/` is the previous implementation,
kept on this branch pending a decision; nothing consumes it.

## Tech Stack

* Python 3.9+, async throughout
* azure-identity (credentials)
* msgraph-core (the supported middleware pipeline)
* unittest with IsolatedAsyncioTestCase, and httpx.MockTransport as the seam

Deliberately NOT msgraph-sdk: its dependency tree does not resolve in practice.

## Project Structure

```text
python/
├── msgraph_simple/
│   ├── __init__.py       public surface: GraphClient, GraphError, PendingSignIn, Scopes
│   ├── _http.py          the middleware pipeline, auth attachment, concurrency limit
│   ├── _auth.py          credentials, PKCE, the loopback redirect listener
│   ├── _request.py       URL building, OData, the response header allowlist
│   ├── _errors.py        GraphError and the code taxonomy
│   ├── _operations.py    paging, batching, download, the two upload strategies
│   ├── _log.py           opt-in structured logging
│   ├── _scopes.py        named permissions
│   └── _resources/       base.py, mail.py, calendar.py
└── tests/

samples/   runnable scripts
docs/      troubleshooting

src/ tests/ build/        the previous C# implementation; nothing consumes it
```

Keep `python/msgraph_simple/` limited to the production code required by the package.

Add files and folders only when the implementation requires them.

### Authentication/

Contains Microsoft Entra ID credential and authentication handling.

Use Microsoft's supported authentication libraries, primarily `Azure.Identity`.

Support applicable authentication scenarios including:

* Delegated authentication
* Client credentials
* Managed identity
* On-behalf-of authentication

Use `TokenCredential` as the credential abstraction.

Keep credential handling centralized.

### Graph/

Contains Microsoft Graph request and client implementation.

Use the official Microsoft Graph SDK for typed operations and Microsoft-supported HTTP infrastructure.

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

Use `IAsyncEnumerable<T>` where it provides a natural interface for paginated operations.

### Models/

Contains only models required by the package implementation, such as Graph error information.

Keep models focused on package behavior and avoid creating models without a concrete use.

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

Use Microsoft's official Graph SDK and supporting libraries where they provide the required functionality.

Use `v1.0` by default.

Support `beta` explicitly when functionality is only available there.

Provide generic request handling so Graph endpoints can be accessed without requiring a dedicated implementation for every endpoint.

## Requests

Support the HTTP methods and request patterns required by Microsoft Graph.

Every asynchronous operation accepts and propagates:

```csharp
CancellationToken
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
* The exact payloads the mail and calendar resources build
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
cd python && python -m unittest discover -s tests
```

Install for development:

```bash
pip install -e python/
```

Build the wheel:

```bash
python -m build --wheel python/
```

## Guiding Principle

Build the smallest reliable Python package that makes Microsoft Graph plug and play: one call to
send a mail, one to book a meeting, and nothing to configure.

