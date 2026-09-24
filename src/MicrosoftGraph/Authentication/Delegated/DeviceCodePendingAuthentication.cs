using Azure.Core;
using Azure.Core.Pipeline;
using Azure.Identity;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>
/// Device code as a two-phase flow (§7.5). <see cref="BeginAsync"/> starts token acquisition on a
/// background task and captures Microsoft's device-code callback, returning as soon as the code is
/// issued. <see cref="CompleteAsync"/> awaits that task, which resolves when the user finishes.
/// </summary>
internal sealed class DeviceCodePendingAuthentication(
    string tenantId,
    string clientId,
    Uri authorityHost,
    IReadOnlyList<string> scopes,
    HttpMessageHandler? transport = null)
    : PendingAuthentication
{
    private readonly TaskCompletionSource<DeviceCodeInfo> _codeIssued =
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    private readonly CancellationTokenSource _lifetime = new();

    public override IReadOnlyList<string> Scopes => scopes;

    private DeviceCodeCredential? _credential;
    private Task<AuthenticationRecord>? _signIn;

    public override async Task<BeginResultEnvelope> BeginAsync(CancellationToken cancellationToken)
    {
        var options = new DeviceCodeCredentialOptions
        {
            TenantId = tenantId,
            ClientId = clientId,
            AuthorityHost = authorityHost,
            DeviceCodeCallback = (info, _) =>
            {
                _codeIssued.TrySetResult(info);
                return Task.CompletedTask;
            },
        };

        if (transport is not null)
        {
            // Test seam: the same HttpMessageHandler abstraction GraphSession uses (§4).
            options.Transport = new HttpClientTransport(new HttpClient(transport));
        }

        _credential = new DeviceCodeCredential(options);

        // AuthenticateAsync does not return until the user finishes, so it runs unawaited and the
        // callback above is what makes the code available now. Its lifetime token is the one
        // graph_auth_cancel trips, so an abandoned sign-in cannot pin a polling task forever.
        _signIn = _credential.AuthenticateAsync(
            new TokenRequestContext([.. scopes]), _lifetime.Token);

        var info = await AwaitCodeAsync(cancellationToken).ConfigureAwait(false);
        ExpiresAt = info.ExpiresOn;

        return new BeginResultEnvelope
        {
            UserCode = info.UserCode,
            VerificationUri = info.VerificationUri.ToString(),
            Message = info.Message,
            ExpiresInSeconds = SecondsUntil(info.ExpiresOn),
        };
    }

    public override async Task<TokenCredential> CompleteAsync(
        string? inputJson, CancellationToken cancellationToken)
    {
        if (_signIn is null || _credential is null)
        {
            throw new GraphCoreException("invalidRequest", "the sign-in was never begun");
        }

        try
        {
            await _signIn.WaitAsync(cancellationToken).ConfigureAwait(false);
            return _credential;
        }
        catch (OperationCanceledException)
        {
            throw new GraphCoreException(
                "signInTimeout", "the user did not complete sign-in within the window");
        }
        catch (AuthenticationFailedException ex)
        {
            throw DelegatedSignIn.Translate(ex);
        }
    }

    public override async ValueTask DisposeAsync()
    {
        await _lifetime.CancelAsync().ConfigureAwait(false);

        // Observe the background task so an abandoned flow cannot surface as an unobserved fault.
        if (_signIn is not null)
        {
            try
            {
                await _signIn.ConfigureAwait(false);
            }
            catch (Exception)
            {
                // Cancelling a sign-in nobody is waiting for is the expected outcome here.
            }
        }

        _lifetime.Dispose();
    }

    /// <summary>
    /// Waits for the code, but not past a sign-in that fails outright — a bad tenant never reaches
    /// the callback, and waiting on it alone would hang until the caller's timeout.
    /// </summary>
    private async Task<DeviceCodeInfo> AwaitCodeAsync(CancellationToken cancellationToken)
    {
        var finished = await Task.WhenAny(_codeIssued.Task, _signIn!)
            .WaitAsync(cancellationToken)
            .ConfigureAwait(false);

        if (finished == _signIn)
        {
            // Faulted before issuing a code; awaiting rethrows the real cause.
            await _signIn.ConfigureAwait(false);
            throw new GraphCoreException(
                "authenticationFailed", "sign-in completed without issuing a device code");
        }

        return await _codeIssued.Task.ConfigureAwait(false);
    }

    internal static int SecondsUntil(DateTimeOffset when) =>
        Math.Max(0, (int)Math.Floor((when - DateTimeOffset.UtcNow).TotalSeconds));
}
