using MicrosoftGraph.Graph.Upload;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Graph.Operations;

/// <summary>
/// Sends a local file to Graph, picking an <see cref="IUploadStrategy"/> by size (§8.7). The
/// template's shared steps — headers, error mapping — still apply, so a failed upload produces the
/// same envelope as any other failure.
/// </summary>
internal sealed class UploadOperation : GraphOperation
{
    /// <summary>Below this a single PUT is fine; at or above it Graph wants an upload session.</summary>
    public const long ChunkedThresholdBytes = 4L * 1024 * 1024;

    private readonly RequestEnvelope _request;
    private readonly FileInfo _file;
    private readonly IUploadStrategy _strategy;

    public UploadOperation(RequestEnvelope request)
        : base(request.Headers)
    {
        _request = request;
        _file = OpenSource(request.SourcePath);
        _strategy = SelectStrategy(_file);
    }

    internal static IUploadStrategy SelectStrategy(FileInfo file) =>
        file.Length < ChunkedThresholdBytes
            ? new SimpleUploadStrategy(file)
            : new ChunkedUploadStrategy(file);

    protected override HttpRequestMessage BuildRequest(GraphUrlBuilder urls) =>
        _strategy.BuildInitialRequest(urls, _request.Path, _request.Version);

    protected override Task<HttpResponseMessage> SendAsync(
        HttpClient http, HttpRequestMessage request, CancellationToken cancellationToken) =>
        _strategy.SendAsync(http, request, cancellationToken);

    protected override async Task<ResponseEnvelope> MapSuccessAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var body = await ReadJsonBodyAsync(response, cancellationToken).ConfigureAwait(false);

        return new ResponseEnvelope
        {
            Ok = true,
            Status = (int)response.StatusCode,
            Headers = ResponseHeaderFilter.Apply(response),
            Body = body,
            BytesSent = _file.Length,
        };
    }

    private static FileInfo OpenSource(string? sourcePath)
    {
        if (string.IsNullOrWhiteSpace(sourcePath))
        {
            throw new GraphCoreException("invalidRequest", "'sourcePath' is required");
        }

        var file = new FileInfo(sourcePath);

        return file.Exists
            ? file
            : throw new GraphCoreException("invalidRequest", $"'{sourcePath}' does not exist");
    }
}
