using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

internal static class OperationRunner
{
    /// <summary>Runs one operation over a stubbed transport and returns what came back.</summary>
    public static async Task<ResponseEnvelope> RunAsync(
        GraphOperation operation, StubHttpMessageHandler transport)
    {
        using var http = new HttpClient(transport);
        var executor = new GraphRequestExecutor(http, new GraphUrlBuilder());
        return await executor.ExecuteAsync(operation, CancellationToken.None);
    }

    public static Task<ResponseEnvelope> RunAsync(
        RequestEnvelope request, StubHttpMessageHandler transport) =>
        RunAsync(new JsonRequestOperation(request), transport);
}
