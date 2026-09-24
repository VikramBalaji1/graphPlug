using Microsoft.Kiota.Http.HttpClientLibrary.Middleware;

namespace MicrosoftGraph.Models.Envelopes;

/// <summary>Inbound credentials (§6.5). The <c>type</c> field selects the strategy.</summary>
/// <remarks>Every field is nullable because this crosses a trust boundary: missing values are
/// reported as <c>invalidRequest</c> naming the field, not as a deserialisation failure.</remarks>
internal sealed record CredentialsEnvelope
{
    public string? Type { get; init; }
    public string? TenantId { get; init; }
    public string? ClientId { get; init; }
    public string? ClientSecret { get; init; }
    public string[]? Scopes { get; init; }
    public string? RedirectUri { get; init; }
    public string? AuthorityHost { get; init; }
    public RetrySettings? Retry { get; init; }
}

/// <summary>
/// Optional per-session retry tuning, mapped onto Kiota's <c>RetryHandlerOption</c> (§8.1).
/// </summary>
/// <remarks>
/// Neither knob is what keeps a caller in Graph's good books. That is
/// <see cref="RetryHandler"/> honouring <c>Retry-After</c>: when Graph throttles, it says how long
/// to wait and the pipeline waits exactly that long. These two only bound how long the waiting may
/// go on before the failure is handed back instead.
/// </remarks>
internal sealed record RetrySettings
{
    /// <summary>
    /// Attempts after the first, per request. Kiota's default is 3. Lower it to fail faster;
    /// raising it makes a throttled request wait longer, not hammer harder — each retry still
    /// waits out the <c>Retry-After</c> Graph asked for.
    /// </summary>
    public int? MaxRetries { get; init; }

    /// <summary>
    /// A ceiling on the <b>total</b> time spent retrying one request, mapped onto
    /// <c>RetriesTimeLimit</c>. Past it the pipeline stops and the failure is returned with its
    /// own <c>retryAfterSeconds</c>, so the caller can decide what to do.
    /// </summary>
    /// <remarks>
    /// This is a total, not a per-attempt cap. <c>RetryHandlerOption.Delay</c> is the per-attempt
    /// base and is deliberately left at Kiota's default, because overriding it would mean
    /// second-guessing the interval Graph itself asked for.
    /// </remarks>
    public int? MaxDelaySeconds { get; init; }
}
