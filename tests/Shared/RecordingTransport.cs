using System.Net;
using System.Text;

namespace TestSupport;

/// <summary>
/// The seam the tests actually need (ARCHITECTURE.md §4): a stubbed transport, so every strategy
/// and operation is exercised without the network and without going near the ABI.
/// </summary>
/// <remarks>
/// Linked into both test projects rather than duplicated. Responses are served in order and the
/// last one repeats, so a single-response script covers the common case and a multi-response one
/// covers retries.
/// </remarks>
internal sealed class RecordingTransport : HttpMessageHandler
{
    private readonly Queue<HttpResponseMessage> _scripted;
    private readonly HttpResponseMessage _last;

    public RecordingTransport(params HttpResponseMessage[] responses)
    {
        if (responses.Length == 0)
        {
            throw new ArgumentException("at least one response is required", nameof(responses));
        }

        _scripted = new Queue<HttpResponseMessage>(responses);
        _last = responses[^1];
    }

    public List<RecordedRequest> Requests { get; } = [];

    public Uri? RequestUri => Requests.Count > 0 ? Requests[^1].Uri : null;

    public string? RequestBody => Requests.Count > 0 ? Requests[^1].Body : null;

    public IReadOnlyDictionary<string, string> RequestHeaders =>
        Requests.Count > 0 ? Requests[^1].Headers : new Dictionary<string, string>();

    /// <summary>One canned response, with optional headers.</summary>
    public static RecordingTransport Returning(
        HttpStatusCode status, string? json = null, params (string Name, string Value)[] headers) =>
        new(Response(status, json, headers));

    public static HttpResponseMessage Response(
        HttpStatusCode status, string? json = null, params (string Name, string Value)[] headers)
    {
        var response = new HttpResponseMessage(status);
        if (json is not null)
        {
            response.Content = new StringContent(json, Encoding.UTF8, "application/json");
        }

        foreach (var (name, value) in headers)
        {
            response.Headers.TryAddWithoutValidation(name, value);
        }

        return response;
    }

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        // Snapshotted, because the operation disposes the request once it returns.
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var (name, values) in request.Headers)
        {
            headers[name] = string.Join(", ", values);
        }

        string? body = null;
        if (request.Content is not null)
        {
            foreach (var (name, values) in request.Content.Headers)
            {
                headers[name] = string.Join(", ", values);
            }

            body = await request.Content.ReadAsStringAsync(cancellationToken);
        }

        Requests.Add(new RecordedRequest(
            request.RequestUri!, headers, body, request.Headers.Authorization?.ToString()));

        var response = _scripted.Count > 0 ? _scripted.Dequeue() : _last;

        // HttpClientHandler sets this, and the retry handlers rebuild the retried request from it.
        response.RequestMessage = request;
        return response;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _last.Dispose();
            while (_scripted.Count > 0)
            {
                _scripted.Dequeue().Dispose();
            }
        }

        base.Dispose(disposing);
    }

    internal sealed record RecordedRequest(
        Uri Uri, Dictionary<string, string> Headers, string? Body, string? Authorization);
}
