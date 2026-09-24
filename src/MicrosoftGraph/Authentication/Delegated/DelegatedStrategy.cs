using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>
/// What every delegated flow has in common: a public client, an interactive two-phase sign-in, and
/// the refusal to default scopes.
/// </summary>
internal abstract class DelegatedStrategy : AuthenticationStrategy
{
    protected DelegatedStrategy(CredentialsEnvelope credentials)
    {
        TenantId = Require(credentials.TenantId, "tenantId");
        ClientId = Require(credentials.ClientId, "clientId");
        AuthorityHost = ParseAuthorityHost(credentials.AuthorityHost) ?? DefaultAuthorityHost;

        // There is no safe default here. `.default` in a delegated flow silently requests every
        // scope ever consented for that client, which is the opposite of least privilege (§7.3).
        Scopes = credentials.Scopes is { Length: > 0 } scopes
            ? scopes
            : throw new GraphCoreException(
                "invalidRequest", "'scopes' is required for delegated authentication");
    }

    private static readonly Uri DefaultAuthorityHost = new("https://login.microsoftonline.com");

    public override IReadOnlyList<string> Scopes { get; }

    protected string TenantId { get; }

    protected string ClientId { get; }

    protected Uri AuthorityHost { get; }

    /// <summary>The tenant-scoped authority MSAL and Azure.Identity both expect.</summary>
    protected Uri Authority => new(AuthorityHost, TenantId);
}
