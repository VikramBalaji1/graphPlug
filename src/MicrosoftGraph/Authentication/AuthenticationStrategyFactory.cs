using MicrosoftGraph.Authentication.AppOnly;
using MicrosoftGraph.Authentication.Delegated;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication;

/// <summary>The single place in the core that knows the full set of auth types (§7.4).</summary>
internal static class AuthenticationStrategyFactory
{
    public static AuthenticationStrategy Create(CredentialsEnvelope credentials) => credentials.Type switch
    {
        "clientSecret" => new ClientSecretStrategy(credentials),
        "deviceCode" => new DeviceCodeStrategy(credentials),
        "authorizationCode" => new AuthorizationCodeStrategy(credentials),
        null or "" => throw new GraphCoreException("invalidRequest", "'type' is required"),
        _ => throw new GraphCoreException(
            "unsupportedCredentialType", $"unknown type '{credentials.Type}'"),
    };
}
