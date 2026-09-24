using Azure.Core;
using Microsoft.Identity.Client;
using MicrosoftGraph.Models;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>
/// A <see cref="TokenCredential"/> over MSAL's token cache, so everything downstream of the
/// credential stays identical across access models (§8.1).
/// </summary>
/// <remarks>
/// Azure.Identity's <c>AuthorizationCodeCredential</c> cannot serve this flow: it exposes no PKCE
/// code verifier, and every constructor demands a client secret that a public client does not have
/// (§7.2). §10 names this MSAL wrapper as the fallback, and this is it.
/// <para>
/// No token handling is written here either — no expiry arithmetic and no refresh timer. MSAL's
/// cache was seeded by the code exchange, and <c>AcquireTokenSilent</c> refreshes from it (§7.6).
/// </para>
/// </remarks>
internal sealed class MsalAuthorizationCodeCredential(
    IConfidentialClientApplication application, IAccount account, IReadOnlyList<string> scopes)
    : TokenCredential
{
    public override AccessToken GetToken(
        TokenRequestContext requestContext, CancellationToken cancellationToken) =>
        GetTokenAsync(requestContext, cancellationToken).AsTask().GetAwaiter().GetResult();

    public override async ValueTask<AccessToken> GetTokenAsync(
        TokenRequestContext requestContext, CancellationToken cancellationToken)
    {
        var requested = requestContext.Scopes is { Length: > 0 } fromCaller ? fromCaller : [.. scopes];

        try
        {
            var result = await application
                .AcquireTokenSilent(requested, account)
                .ExecuteAsync(cancellationToken)
                .ConfigureAwait(false);

            return new AccessToken(result.AccessToken, result.ExpiresOn);
        }
        catch (MsalUiRequiredException ex)
        {
            // The refresh token is gone or revoked. The handle cannot recover on its own — the
            // user has to sign in again, which means a new client.
            throw new GraphCoreException(
                "interactionRequired",
                $"the signed-in session can no longer be refreshed: {ex.Message}");
        }
        catch (MsalException ex)
        {
            throw DelegatedSignIn.Translate(ex);
        }
    }
}
