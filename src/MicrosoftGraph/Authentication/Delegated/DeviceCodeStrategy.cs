using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>
/// Delegated access by device code (§7.1). Fits headless scripts and containers: the user signs in
/// on another device, so this process never needs a browser.
/// </summary>
internal sealed class DeviceCodeStrategy(CredentialsEnvelope credentials)
    : DelegatedStrategy(credentials)
{
    public override PendingAuthentication CreatePendingAuthentication() =>
        new DeviceCodePendingAuthentication(TenantId, ClientId, AuthorityHost, Scopes);
}
