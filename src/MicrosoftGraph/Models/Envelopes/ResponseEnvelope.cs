using System.Text.Json.Nodes;

namespace MicrosoftGraph.Models.Envelopes;

/// <summary>
/// The outcome of an operation that reached Graph (§6.5, §9.1). <c>Ok</c> discriminates:
/// success carries <c>Body</c> and <c>NextLink</c>, failure carries <c>Error</c>.
/// </summary>
internal sealed record ResponseEnvelope
{
    public required bool Ok { get; init; }
    public required int Status { get; init; }
    public Dictionary<string, string>? Headers { get; init; }
    public JsonNode? Body { get; init; }

    /// <summary>Lifted from <c>@odata.nextLink</c> so callers never touch the OData annotation name.</summary>
    public string? NextLink { get; init; }

    /// <summary>Download only.</summary>
    public long? BytesWritten { get; init; }

    /// <summary>Download only: where the file actually landed.</summary>
    public string? DestPath { get; init; }

    /// <summary>Upload only.</summary>
    public long? BytesSent { get; init; }

    public GraphErrorInfo? Error { get; init; }
}
