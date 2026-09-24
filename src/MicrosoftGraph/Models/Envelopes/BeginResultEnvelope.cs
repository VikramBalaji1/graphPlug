namespace MicrosoftGraph.Models.Envelopes;

/// <summary>
/// What <c>graph_auth_begin</c> returns: the flow handle, plus whatever the user must see or do.
/// Which fields are populated depends on the flow; the rest are omitted.
/// </summary>
internal sealed record BeginResultEnvelope
{
    public bool Ok => true;

    /// <summary>Assigned by the registry once the flow is begun; passed back to complete or cancel.</summary>
    public long FlowId { get; init; }

    /// <summary>Device code: the code the user types.</summary>
    public string? UserCode { get; init; }

    /// <summary>Device code: where the user types it.</summary>
    public string? VerificationUri { get; init; }

    /// <summary>Device code: Microsoft's own instruction text, suitable for printing verbatim.</summary>
    public string? Message { get; init; }

    /// <summary>Authorization code: the URL to open in a browser.</summary>
    public string? AuthorizeUrl { get; init; }

    /// <summary>Authorization code: the anti-forgery value the redirect must echo back.</summary>
    public string? State { get; init; }

    public int ExpiresInSeconds { get; init; }
}
