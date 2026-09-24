namespace MicrosoftGraph.Graph.Upload;

/// <summary>A single <c>PUT</c> of the file stream. Used below 4 MiB (§8.7).</summary>
internal sealed class SimpleUploadStrategy(FileInfo file) : IUploadStrategy
{
    public HttpRequestMessage BuildInitialRequest(GraphUrlBuilder urls, string? path, string? version)
    {
        var stream = file.OpenRead();
        var content = new StreamContent(stream);
        content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue(
            "application/octet-stream");

        return new HttpRequestMessage(HttpMethod.Put, urls.Build(path, version, query: null))
        {
            Content = content,
        };
    }

    public Task<HttpResponseMessage> SendAsync(
        HttpClient http, HttpRequestMessage request, CancellationToken cancellationToken) =>
        http.SendAsync(request, cancellationToken);
}
