# CLAUDE.md

## Overview

A minimal C#/.NET package for unified Microsoft Entra ID authentication and Microsoft Graph API request handling.

C# is the canonical implementation. The package will later serve as the core for a Python developer interface.

## Tech Stack

* .NET 10
* C#
* Microsoft Graph SDK for .NET
* Azure.Identity
* xUnit
* FluentAssertions
* NuGet

## Project Structure

```text
src/
└── MicrosoftGraph/
    ├── Authentication/
    ├── Graph/
    ├── Models/
    └── MicrosoftGraph.csproj

tests/
├── UnitTests/
└── IntegrationTests/

docs/
samples/
```

Keep `src/MicrosoftGraph/` limited to the production code required by the package.

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

Avoid implementing custom infrastructure when the Microsoft Graph SDK or .NET already provides the required behavior.

## Testing

Use xUnit and FluentAssertions.

Unit tests cover:

* Authentication behavior
* Request construction
* Graph responses
* Pagination
* Error handling
* Retry behavior
* Serialization

Integration tests verify realistic Graph HTTP interactions using controlled responses.

Live Graph tests use dedicated test environments and credentials.

## Dependencies

Keep dependencies minimal.

Prefer:

* .NET platform APIs
* Azure.Identity
* Microsoft Graph SDK

Use additional dependencies only when they provide functionality required by the package.

## Package

NuGet is the primary package artifact.

The C# implementation is the source of truth for authentication and Microsoft Graph behavior.

The future Python interface consumes this C# core rather than reimplementing authentication or Graph functionality.

## Development

Build:

```bash
dotnet build
```

Test:

```bash
dotnet test
```

Format:

```bash
dotnet format
```

Pack:

```bash
dotnet pack -c Release
```

## Guiding Principle

Build the smallest reliable C# core that provides unified Microsoft Entra ID authentication and Microsoft Graph request handling.

