using System.Net;

namespace UnitTests;

/// <summary>
/// The seam the tests actually need (ARCHITECTURE.md §4): a stubbed transport, so every strategy
/// and operation is exercised without the network and without going near the ABI.
/// </summary>
internal sealed class StubHttpMessageHandler(HttpResponseMessage response) : HttpMessageHandler
{
    public Uri? RequestUri { get; private set; }

    public string? RequestBody { get; private set; }

    /// <summary>Snapshotted at send time, because the operation disposes the request afterwards.</summary>
    public Dictionary<string, string> RequestHeaders { get; } = new(StringComparer.OrdinalIgnoreCase);

    public static StubHttpMessageHandler Returning(
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

        return new StubHttpMessageHandler(response);
    }

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        RequestUri = request.RequestUri;

        foreach (var (name, values) in request.Headers)
        {
            RequestHeaders[name] = string.Join(", ", values);
        }

        if (request.Content is not null)
        {
            foreach (var (name, values) in request.Content.Headers)
            {
                RequestHeaders[name] = string.Join(", ", values);
            }

            RequestBody = await request.Content.ReadAsStringAsync(cancellationToken);
        }

        return response;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            response.Dispose();
        }

        base.Dispose(disposing);
    }
}
