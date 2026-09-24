using Azure.Core;
using MicrosoftGraph.Models;

namespace MicrosoftGraph.Authentication;

/// <summary>Which Entra access model a strategy implements (§7.1).</summary>
internal enum AccessModel
{
    AppOnly,
    Delegated,
}

/// <summary>
/// One Entra sign-in method (§7.4). Adding a flow is a new subclass plus one case in
/// <see cref="AuthenticationStrategyFactory"/>; nothing else in the core moves.
/// </summary>
internal abstract class AuthenticationStrategy
{
    public abstract AccessModel AccessModel { get; }

    public abstract bool RequiresInteraction { get; }

    /// <summary>Scopes to request. App-only resolves to <c>.default</c>; delegated must be explicit (§7.3).</summary>
    public abstract IReadOnlyList<string> Scopes { get; }

    /// <summary>Single-shot: credentials in, credential out. Non-interactive flows only.</summary>
    public virtual Task<TokenCredential> CreateCredentialAsync(CancellationToken cancellationToken) =>
        throw new GraphCoreException(
            "interactionRequired",
            $"'{GetType().Name}' requires the two-phase sign-in flow");

    /// <summary>Two-phase: returns a flow that must be begun and then completed (§7.5).</summary>
    public virtual PendingAuthentication CreatePendingAuthentication() =>
        throw new GraphCoreException(
            "interactionNotSupported",
            $"'{GetType().Name}' does not require interaction");

    protected static string Require(string? value, string field) =>
        string.IsNullOrWhiteSpace(value)
            ? throw new GraphCoreException("invalidRequest", $"'{field}' is required")
            : value;

    protected static Uri? ParseAuthorityHost(string? authorityHost)
    {
        if (string.IsNullOrWhiteSpace(authorityHost))
        {
            return null;
        }

        return Uri.TryCreate(authorityHost, UriKind.Absolute, out var uri) && uri.Scheme == Uri.UriSchemeHttps
            ? uri
            : throw new GraphCoreException("invalidRequest", "'authorityHost' must be an absolute https URL");
    }
}
