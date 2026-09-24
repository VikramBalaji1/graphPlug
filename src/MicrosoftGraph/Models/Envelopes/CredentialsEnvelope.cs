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

/// <summary>Optional per-session retry tuning, mapped onto Kiota's <c>RetryHandlerOption</c> (§8.1).</summary>
internal sealed record RetrySettings
{
    public int? MaxRetries { get; init; }
    public int? MaxDelaySeconds { get; init; }
}
