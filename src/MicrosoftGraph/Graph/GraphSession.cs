using Azure.Core;
using Microsoft.Graph;
using Microsoft.Graph.Authentication;
using Microsoft.Kiota.Http.HttpClientLibrary.Middleware;
using Microsoft.Kiota.Http.HttpClientLibrary.Middleware.Options;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Graph;

/// <summary>
/// One handle's worth of state: a credential, an <see cref="HttpClient"/> built from Microsoft's
/// handler pipeline, and the executor that runs operations over it (§8.1). Safe to share across
/// threads — <see cref="HttpClient"/> is thread-safe and Azure.Identity serialises token refresh.
/// </summary>
internal sealed class GraphSession : IAsyncDisposable
{
    /// <summary>
    /// The only host the bearer token may be attached to. Absolute URLs off this host — a
    /// pre-authenticated download URL, say — still work, they just travel unauthenticated.
    /// </summary>
    private const string GraphHost = "graph.microsoft.com";

    private readonly HttpClient _http;

    private GraphSession(HttpClient http)
    {
        _http = http;
        Executor = new GraphRequestExecutor(http, new GraphUrlBuilder());
    }

    public GraphRequestExecutor Executor { get; }

    /// <summary>
    /// Builds the session identically for both access models — once a <see cref="TokenCredential"/>
    /// exists, nothing downstream knows or cares how it was obtained.
    /// </summary>
    /// <param name="finalHandler">Test seam: the innermost handler, replacing the network.</param>
    public static GraphSession Create(
        TokenCredential credential,
        IReadOnlyList<string> scopes,
        RetrySettings? retry = null,
        HttpMessageHandler? finalHandler = null)
    {
        var handlers = GraphClientFactory.CreateDefaultHandlers();
        ApplyRetryOptions(handlers, retry);

        var authenticationProvider = new AzureIdentityAuthenticationProvider(
            credential,
            allowedHosts: [GraphHost],
            observabilityOptions: null,
            isCaeEnabled: true,
            scopes: [.. scopes]);
        handlers.Add(new AuthorizationHandler(authenticationProvider));

        return new GraphSession(GraphClientFactory.Create(handlers, finalHandler: finalHandler));
    }

    public ValueTask DisposeAsync()
    {
        _http.Dispose();
        return ValueTask.CompletedTask;
    }

    /// <summary>
    /// Replaces the pipeline's default <c>RetryHandler</c> when the caller tuned it. No retry or
    /// backoff logic is written here — only the knobs on Microsoft's handler are set (§8.1).
    /// </summary>
    private static void ApplyRetryOptions(IList<DelegatingHandler> handlers, RetrySettings? retry)
    {
        if (retry is null || (retry.MaxRetries is null && retry.MaxDelaySeconds is null))
        {
            return;
        }

        var option = new RetryHandlerOption();
        if (retry.MaxRetries is { } maxRetries)
        {
            option.MaxRetry = maxRetries;
        }

        // `maxDelaySeconds` is read as a ceiling on total time spent retrying, which is
        // RetriesTimeLimit; RetryHandlerOption.Delay is a per-attempt base, not a maximum.
        if (retry.MaxDelaySeconds is { } maxDelaySeconds)
        {
            option.RetriesTimeLimit = TimeSpan.FromSeconds(maxDelaySeconds);
        }

        for (var i = 0; i < handlers.Count; i++)
        {
            if (handlers[i] is RetryHandler existing)
            {
                existing.Dispose();
                handlers[i] = new RetryHandler(option);
                return;
            }
        }
    }
}
