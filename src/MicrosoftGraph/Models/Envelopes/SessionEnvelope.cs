namespace MicrosoftGraph.Models.Envelopes;

/// <summary>Returned by <c>graph_client_create</c> and <c>graph_client_close</c> (§6.6).</summary>
internal sealed record SessionEnvelope
{
    public required bool Ok { get; init; }
    public long Handle { get; init; }

    /// <summary>Compared against the wheel's own version at load time; a mismatch must fail loudly.</summary>
    public string? CoreVersion { get; init; }
}
