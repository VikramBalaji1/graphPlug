using Azure.Core;
using Azure.Identity;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication.AppOnly;

/// <summary>
/// Application-level access via a client secret (§7.1). The app acts as itself, with the
/// application permissions an administrator consented to — tenant-wide, not scoped to a user.
/// </summary>
internal sealed class ClientSecretStrategy : AuthenticationStrategy
{
    public const string DefaultScope = "https://graph.microsoft.com/.default";

    private readonly string _tenantId;
    private readonly string _clientId;
    private readonly string _clientSecret;
    private readonly Uri? _authorityHost;

    public ClientSecretStrategy(CredentialsEnvelope credentials)
    {
        _tenantId = Require(credentials.TenantId, "tenantId");
        _clientId = Require(credentials.ClientId, "clientId");
        _clientSecret = Require(credentials.ClientSecret, "clientSecret");
        _authorityHost = ParseAuthorityHost(credentials.AuthorityHost);

        // Scope minimisation belongs in the app registration, so `.default` is the right default
        // here: it resolves to exactly the consented application permissions and nothing more.
        Scopes = credentials.Scopes is { Length: > 0 } scopes ? scopes : [DefaultScope];
    }

    public override AccessModel AccessModel => AccessModel.AppOnly;

    public override bool RequiresInteraction => false;

    public override IReadOnlyList<string> Scopes { get; }

    public override Task<TokenCredential> CreateCredentialAsync(CancellationToken cancellationToken)
    {
        var options = new ClientSecretCredentialOptions();
        if (_authorityHost is not null)
        {
            options.AuthorityHost = _authorityHost;
        }

        return Task.FromResult<TokenCredential>(
            new ClientSecretCredential(_tenantId, _clientId, _clientSecret, options));
    }
}
