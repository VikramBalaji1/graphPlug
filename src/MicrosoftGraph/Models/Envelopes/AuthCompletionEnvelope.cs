namespace MicrosoftGraph.Models.Envelopes;

/// <summary>
/// The input to <c>graph_auth_complete</c>. Device code needs nothing; the authorization-code flow
/// supplies what the redirect delivered.
/// </summary>
internal sealed record AuthCompletionEnvelope
{
    public string? Code { get; init; }

    public string? State { get; init; }
}
