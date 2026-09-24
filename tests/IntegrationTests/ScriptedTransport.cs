using System.Net;

namespace IntegrationTests;

/// <summary>
/// A controlled transport at the very bottom of the real handler pipeline. Each call takes the
/// next scripted response, so retries are visible as extra recorded requests.
/// </summary>
internal sealed class ScriptedTransport(params HttpResponseMessage[] responses) : HttpMessageHandler
{
    private readonly Queue<HttpResponseMessage> _remaining = new(responses);

    public List<RecordedRequest> Requests { get; } = [];

    public static HttpResponseMessage Response(
        HttpStatusCode status, string? json = null, params (string Name, string Value)[] headers)
    {
        var response = new HttpResponseMessage(status);
        if (json is not null)
        {
            response.Content = new StringContent(json, System.Text.Encoding.UTF8, "application/json");
        }

        foreach (var (name, value) in headers)
        {
            response.Headers.TryAddWithoutValidation(name, value);
        }

        return response;
    }

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        Requests.Add(new RecordedRequest(
            request.RequestUri!,
            request.Headers.Authorization?.ToString()));

        if (_remaining.Count == 0)
        {
            throw new InvalidOperationException("the transport ran out of scripted responses");
        }

        var response = _remaining.Dequeue();

        // HttpClientHandler sets this, and the retry handlers rebuild the retried request from it.
        response.RequestMessage = request;
        return Task.FromResult(response);
    }

    internal sealed record RecordedRequest(Uri Uri, string? Authorization);
}
