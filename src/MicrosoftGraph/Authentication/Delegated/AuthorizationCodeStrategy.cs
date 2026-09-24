using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>
/// Delegated access by authorization code with PKCE (§7.1). Gives the interactive-browser
/// experience while the core does only the NativeAOT-safe half — Python owns the browser and the
/// redirect, the core owns the verifier and the exchange (§11.3).
/// </summary>
internal sealed class AuthorizationCodeStrategy : DelegatedStrategy
{
    private readonly string _redirectUri;

    public AuthorizationCodeStrategy(CredentialsEnvelope credentials)
        : base(credentials)
    {
        _redirectUri = ParseRedirectUri(credentials.RedirectUri);
    }

    public override PendingAuthentication CreatePendingAuthentication() =>
        new AuthorizationCodePendingAuthentication(ClientId, Authority, _redirectUri, Scopes);

    /// <summary>
    /// Loopback only. A redirect URI pointing anywhere else would send the authorization code off
    /// this machine, and the listener that catches it binds to loopback regardless (§11.3).
    /// </summary>
    /// <returns>
    /// The caller's own string, not a round-tripped <see cref="Uri"/>. Entra requires the value to
    /// match the app registration character for character, and <c>Uri.ToString()</c> would append a
    /// trailing slash that turns a registered <c>http://localhost:8400</c> into AADSTS50011.
    /// </returns>
    private static string ParseRedirectUri(string? redirectUri)
    {
        if (string.IsNullOrWhiteSpace(redirectUri))
        {
            throw new GraphCoreException(
                "invalidRequest", "'redirectUri' is required for the authorization code flow");
        }

        if (!Uri.TryCreate(redirectUri, UriKind.Absolute, out var parsed))
        {
            throw new GraphCoreException("invalidRequest", "'redirectUri' must be an absolute URL");
        }

        return parsed.IsLoopback
            ? redirectUri
            : throw new GraphCoreException(
                "invalidRequest", "'redirectUri' must be a loopback address");
    }
}
