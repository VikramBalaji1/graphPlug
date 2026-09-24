namespace MicrosoftGraph.Graph.Upload;

/// <summary>
/// How a file's bytes reach Graph (§8.7). The two paths share nothing but their input, which is
/// exactly what the Strategy pattern is for.
/// </summary>
internal interface IUploadStrategy
{
    /// <summary>The first request of the upload — the whole thing, or the session that starts it.</summary>
    HttpRequestMessage BuildInitialRequest(GraphUrlBuilder urls, string? path, string? version);

    /// <summary>
    /// Sends <paramref name="request"/> and whatever must follow it, returning the response that
    /// decides the outcome. Non-success responses are mapped by the shared error path, so a failed
    /// upload produces the same envelope shape as any other failure.
    /// </summary>
    Task<HttpResponseMessage> SendAsync(
        HttpClient http, HttpRequestMessage request, CancellationToken cancellationToken);
}
