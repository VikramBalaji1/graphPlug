using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Azure.Core;
using Microsoft.Identity.Client;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>
/// Authorization code with PKCE as a two-phase flow (§7.5). <see cref="BeginAsync"/> generates the
/// verifier and challenge and builds the authorize URL — no network call, nothing to wait for.
/// Python opens a browser and catches the redirect; <see cref="CompleteAsync"/> validates
/// <c>state</c> and exchanges the code.
/// </summary>
/// <remarks>
/// Python never sees the verifier and never touches a token — it shuttles an authorization code,
/// which is single-use, short-lived and worthless without the verifier the core kept.
/// </remarks>
internal sealed class AuthorizationCodePendingAuthentication(
    string clientId,
    Uri authority,
    string redirectUri,
    IReadOnlyList<string> scopes)
    : PendingAuthentication
{
    /// <summary>
    /// Requested on the authorize URL so the session can refresh silently for the life of the
    /// handle (§7.6). It is deliberately <b>not</b> passed to MSAL's token calls, which reject the
    /// reserved scopes — MSAL adds them itself.
    /// </summary>
    private const string OfflineAccess = "offline_access";

    public override IReadOnlyList<string> Scopes => scopes;

    private PkceCodes? _pkce;
    private string? _state;

    public override Task<BeginResultEnvelope> BeginAsync(CancellationToken cancellationToken)
    {
        _pkce = PkceCodes.Create();
        _state = PkceCodes.CreateState();
        ExpiresAt = DateTimeOffset.UtcNow + DelegatedSignIn.Window;

        return Task.FromResult(new BeginResultEnvelope
        {
            AuthorizeUrl = BuildAuthorizeUrl(_pkce.Challenge, _state),
            State = _state,
            ExpiresInSeconds = (int)DelegatedSignIn.Window.TotalSeconds,
        });
    }

    public override async Task<TokenCredential> CompleteAsync(
        string? inputJson, CancellationToken cancellationToken)
    {
        if (_pkce is null || _state is null)
        {
            throw new GraphCoreException("invalidRequest", "the sign-in was never begun");
        }

        var completion = ParseCompletion(inputJson);
        RequireMatchingState(completion.State, _state);

        var code = string.IsNullOrWhiteSpace(completion.Code)
            ? throw new GraphCoreException("invalidRequest", "'code' is required")
            : completion.Code;

        // A public client holds no secret, and MSAL permits exactly that for a PKCE exchange.
        var application = ConfidentialClientApplicationBuilder
            .Create(clientId)
            .WithAuthority(authority)
            .WithRedirectUri(redirectUri)
            .Build();

        try
        {
            var result = await application
                .AcquireTokenByAuthorizationCode([.. scopes], code)
                .WithPkceCodeVerifier(_pkce.Verifier)
                .ExecuteAsync(cancellationToken)
                .ConfigureAwait(false);

            return new MsalAuthorizationCodeCredential(application, result.Account, scopes);
        }
        catch (OperationCanceledException)
        {
            throw new GraphCoreException(
                "signInTimeout", "the user did not complete sign-in within the window");
        }
        catch (MsalException ex)
        {
            throw DelegatedSignIn.Translate(ex);
        }
        finally
        {
            // The verifier has served its purpose; do not leave it reachable for a second attempt.
            _pkce = null;
        }
    }

    public override ValueTask DisposeAsync()
    {
        _pkce = null;
        _state = null;
        return ValueTask.CompletedTask;
    }

    /// <summary>
    /// Compared in fixed time. The window is tiny, but a state check is an anti-forgery control and
    /// those are not the place to leak timing.
    /// </summary>
    private static void RequireMatchingState(string? supplied, string expected)
    {
        var matches = supplied is not null && CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(supplied), Encoding.UTF8.GetBytes(expected));

        if (!matches)
        {
            throw new GraphCoreException(
                "stateMismatch", "the redirect state did not match the one issued");
        }
    }

    private static AuthCompletionEnvelope ParseCompletion(string? inputJson)
    {
        if (string.IsNullOrWhiteSpace(inputJson))
        {
            throw new GraphCoreException("invalidRequest", "'code' and 'state' are required");
        }

        return JsonSerializer.Deserialize(inputJson, GraphJsonContext.Default.AuthCompletionEnvelope)
            ?? throw new GraphCoreException("invalidRequest", "the completion envelope was empty");
    }

    private string BuildAuthorizeUrl(string challenge, string state)
    {
        var requested = string.Join(' ', scopes.Append(OfflineAccess));

        return $"{authority}/oauth2/v2.0/authorize" +
               $"?client_id={Uri.EscapeDataString(clientId)}" +
               "&response_type=code" +
               $"&redirect_uri={Uri.EscapeDataString(redirectUri)}" +
               "&response_mode=query" +
               $"&scope={Uri.EscapeDataString(requested)}" +
               $"&state={Uri.EscapeDataString(state)}" +
               $"&code_challenge={Uri.EscapeDataString(challenge)}" +
               "&code_challenge_method=S256";
    }
}
