using System.Runtime.CompilerServices;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Graph.Operations;

/// <summary>
/// <c>POST /$batch</c> (§8.5). Two pieces of behaviour belong to the core rather than to Python,
/// because they are Graph behaviour and C# is the source of truth for that.
/// </summary>
/// <remarks>
/// <para>
/// <b>Chunking at 20.</b> Graph rejects larger batches, so a bigger set is split, issued and merged.
/// <b>Re-ordering by id.</b> Graph does not guarantee response order within a batch, so input order
/// is restored and results align positionally with what was sent.
/// </para>
/// <para>
/// Per-request failures are not raised: each sub-request keeps its own <c>status</c> and body. One
/// failing sub-request must not discard nineteen successful ones.
/// </para>
/// <para>
/// This is deliberately <b>not</b> a <see cref="GraphOperation"/>, which §8.2 shapes around a single
/// request. A batch is a composite: each chunk is an ordinary <see cref="JsonRequestOperation"/>
/// run through the executor, so chunks inherit retry, throttling and the shared error shape for
/// free, and this class holds only the splitting and merging.
/// </para>
/// </remarks>
internal sealed class BatchOperation(RequestEnvelope request)
{
    /// <summary>Graph's hard limit on requests per batch.</summary>
    public const int MaxRequestsPerChunk = 20;

    public const string BatchPath = "/$batch";

    /// <summary>Recognises the path that means "this is a batch" rather than an ordinary POST.</summary>
    public static bool Matches(RequestEnvelope candidate) =>
        candidate.Path is { } path && path.EndsWith(BatchPath, StringComparison.OrdinalIgnoreCase);

    public async Task<ResponseEnvelope> ExecuteAsync(
        GraphRequestExecutor executor, CancellationToken cancellationToken)
    {
        var requests = ReadRequests();
        var merged = new JsonArray();
        var status = 200;

        await foreach (var chunkResponse in SendChunksAsync(executor, requests, cancellationToken)
            .ConfigureAwait(false))
        {
            if (!chunkResponse.Ok)
            {
                // The batch call itself failed, not a sub-request. Report it as it stands.
                return chunkResponse;
            }

            foreach (var subResponse in Ordered(chunkResponse.Body, requests))
            {
                merged.Add(subResponse);
            }

            status = chunkResponse.Status;
        }

        return new ResponseEnvelope
        {
            Ok = true,
            Status = status,
            Body = new JsonObject { ["responses"] = merged },
        };
    }

    /// <summary>
    /// Chunks are issued sequentially. <c>IAsyncEnumerable</c> reads naturally here and keeps only
    /// one chunk's responses alive at a time.
    /// </summary>
    /// <remarks>
    /// Known ceiling: sequential dispatch, and a <c>dependsOn</c> chain spanning two chunks will
    /// fail at Graph. Both are acceptable at expected volumes; the fix is concurrent dispatch and
    /// dependency-aware partitioning, worth adding only if batch latency is measured as a problem.
    /// </remarks>
    private async IAsyncEnumerable<ResponseEnvelope> SendChunksAsync(
        GraphRequestExecutor executor,
        IReadOnlyList<JsonNode> requests,
        [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        for (var offset = 0; offset < requests.Count; offset += MaxRequestsPerChunk)
        {
            var chunk = new JsonArray();
            var end = Math.Min(offset + MaxRequestsPerChunk, requests.Count);
            for (var i = offset; i < end; i++)
            {
                chunk.Add(requests[i].DeepClone());
            }

            var chunkRequest = request with
            {
                Method = "POST",
                Path = BatchPath,
                Query = null,
                Body = new JsonObject { ["requests"] = chunk },
            };

            yield return await executor
                .ExecuteAsync(new JsonRequestOperation(chunkRequest), cancellationToken)
                .ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Restores submission order. Graph returns sub-responses in whatever order they completed, so
    /// without this a caller could not line results up with what it sent.
    /// </summary>
    private static IEnumerable<JsonNode> Ordered(JsonNode? body, IReadOnlyList<JsonNode> requests)
    {
        var returned = body?["responses"]?.AsArray();
        if (returned is null)
        {
            yield break;
        }

        var byId = new Dictionary<string, JsonNode>(StringComparer.Ordinal);
        foreach (var response in returned)
        {
            if (response is not null && IdOf(response) is { } id)
            {
                byId[id] = response;
            }
        }

        foreach (var sent in requests)
        {
            if (IdOf(sent) is { } id && byId.Remove(id, out var matched))
            {
                yield return matched.DeepClone();
            }
        }

        // Anything Graph returned that was not asked for still reaches the caller rather than
        // being silently dropped.
        foreach (var orphan in byId.Values)
        {
            yield return orphan.DeepClone();
        }
    }

    private static string? IdOf(JsonNode? node) => node?["id"] switch
    {
        null => null,
        var id => id.GetValueKind() == JsonValueKind.String ? id.GetValue<string>() : id.ToJsonString(),
    };

    /// <summary>
    /// Reads the caller's sub-requests, assigning an <c>id</c> to any that lack one so re-ordering
    /// has something to key on.
    /// </summary>
    private IReadOnlyList<JsonNode> ReadRequests()
    {
        var requests = request.Body?["requests"]?.AsArray()
            ?? throw new GraphCoreException(
                "invalidRequest", "a batch body must carry a 'requests' array");

        var prepared = new List<JsonNode>(requests.Count);
        for (var i = 0; i < requests.Count; i++)
        {
            var sub = requests[i]?.DeepClone()
                ?? throw new GraphCoreException("invalidRequest", $"request {i} in the batch is null");

            if (IdOf(sub) is null)
            {
                sub["id"] = i.ToString(CultureInfo.InvariantCulture);
            }

            prepared.Add(sub);
        }

        return prepared;
    }
}
