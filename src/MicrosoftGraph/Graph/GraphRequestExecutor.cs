using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Graph;

/// <summary>
/// Owns the transport an operation runs on, so a <see cref="GraphOperation"/> carries only the
/// description of one request and nothing about where it is sent.
/// </summary>
internal sealed class GraphRequestExecutor(HttpClient http, GraphUrlBuilder urls)
{
    public HttpClient Http { get; } = http;

    public GraphUrlBuilder Urls { get; } = urls;

    public Task<ResponseEnvelope> ExecuteAsync(
        GraphOperation operation, CancellationToken cancellationToken) =>
        operation.ExecuteAsync(this, cancellationToken);

    /// <summary>
    /// Picks the operation a request describes. A batch is a composite rather than a
    /// <see cref="GraphOperation"/>, so the choice lives here rather than at the ABI boundary.
    /// </summary>
    public Task<ResponseEnvelope> ExecuteAsync(
        RequestEnvelope request, CancellationToken cancellationToken) =>
        BatchOperation.Matches(request)
            ? new BatchOperation(request).ExecuteAsync(this, cancellationToken)
            : ExecuteAsync(new JsonRequestOperation(request), cancellationToken);
}
