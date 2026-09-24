using System.Text.Json;
using System.Text.Json.Nodes;

namespace MicrosoftGraph.Models.Envelopes;

/// <summary>Inbound request description (§6.5).</summary>
internal sealed record RequestEnvelope
{
    public const int DefaultTimeoutMs = 100_000;

    public string? Method { get; init; }
    public string? Path { get; init; }
    public string? Version { get; init; }

    /// <summary>OData parameters. Values may be strings or numbers; the core URL-encodes both.</summary>
    public Dictionary<string, JsonElement>? Query { get; init; }

    public Dictionary<string, string>? Headers { get; init; }
    public JsonNode? Body { get; init; }

    /// <summary>Download only: where to write. Binary payloads use the filesystem, not the envelope (D9).</summary>
    public string? DestPath { get; init; }

    /// <summary>Upload only: the local file to send.</summary>
    public string? SourcePath { get; init; }

    public int? TimeoutMs { get; init; }
}
