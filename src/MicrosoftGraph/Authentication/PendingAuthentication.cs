using Azure.Core;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication;

/// <summary>
/// A sign-in that must pause for a human (§7.5). A single synchronous ABI call cannot express
/// that, so the flow is split: <see cref="BeginAsync"/> returns what the user must see or do, and
/// <see cref="CompleteAsync"/> waits for them. No callback ever crosses back into Python.
/// </summary>
internal abstract class PendingAuthentication : IAsyncDisposable
{
    /// <summary>The sign-in window. Past it the registry evicts the flow (§7.5).</summary>
    public DateTimeOffset ExpiresAt { get; protected set; } = DateTimeOffset.MaxValue;

    public bool HasExpired => DateTimeOffset.UtcNow > ExpiresAt;

    /// <summary>The scopes this sign-in was begun for; the session is built with the same set.</summary>
    public abstract IReadOnlyList<string> Scopes { get; }

    /// <summary>Returns what the user must see or do. Fast — no waiting for the human.</summary>
    public abstract Task<BeginResultEnvelope> BeginAsync(CancellationToken cancellationToken);

    /// <summary>Waits for the user to finish, then yields the credential.</summary>
    public abstract Task<TokenCredential> CompleteAsync(string? inputJson, CancellationToken cancellationToken);

    public abstract ValueTask DisposeAsync();
}
