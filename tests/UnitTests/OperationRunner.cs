using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

internal static class OperationRunner
{
    /// <summary>Runs one operation over a stubbed transport and returns what came back.</summary>
    public static async Task<ResponseEnvelope> RunAsync(
        GraphOperation operation, RecordingTransport transport)
    {
        await using var session = new GraphSession(new HttpClient(transport));
        return await session.ExecuteAsync(operation, CancellationToken.None);
    }

    public static Task<ResponseEnvelope> RunAsync(
        RequestEnvelope request, RecordingTransport transport) =>
        RunAsync(new JsonRequestOperation(request), transport);
}
