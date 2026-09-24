using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Graph.Operations;

/// <summary>
/// Streams a Graph response straight to disk (§8.6). The bytes never enter a JSON envelope and
/// never fully enter memory, so file size is bounded by disk rather than RAM (D9).
/// </summary>
internal sealed class DownloadOperation(RequestEnvelope request) : GraphOperation(request.Headers)
{
    private const int CopyBufferBytes = 81920;

    private readonly string _destPath = RequireDestination(request.DestPath);

    protected override HttpRequestMessage BuildRequest(GraphUrlBuilder urls) =>
        new(HttpMethod.Get, urls.Build(request.Path, request.Version, request.Query));

    /// <summary>
    /// Reads only the headers before returning, so the body can be streamed rather than buffered.
    /// </summary>
    protected override Task<HttpResponseMessage> SendAsync(
        HttpClient http, HttpRequestMessage message, CancellationToken cancellationToken) =>
        http.SendAsync(message, HttpCompletionOption.ResponseHeadersRead, cancellationToken);

    protected override async Task<ResponseEnvelope> MapSuccessAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        // Written to a temporary sibling and renamed on success, so a failed or cancelled download
        // cannot leave a truncated file at the destination path.
        var partial = $"{_destPath}.{Guid.NewGuid():N}.partial";
        long written;

        try
        {
            await using (var destination = new FileStream(
                partial, FileMode.CreateNew, FileAccess.Write, FileShare.None, CopyBufferBytes,
                useAsync: true))
            {
                await using var source = await response.Content
                    .ReadAsStreamAsync(cancellationToken)
                    .ConfigureAwait(false);

                await source.CopyToAsync(destination, CopyBufferBytes, cancellationToken)
                    .ConfigureAwait(false);

                written = destination.Length;
            }

            File.Move(partial, _destPath, overwrite: true);
        }
        catch (Exception)
        {
            TryDelete(partial);
            throw;
        }

        return new ResponseEnvelope
        {
            Ok = true,
            Status = (int)response.StatusCode,
            Headers = ResponseHeaderFilter.Apply(response),
            BytesWritten = written,
            DestPath = _destPath,
        };
    }

    /// <summary>The core does not create directories; it says so rather than guessing (§8.6).</summary>
    private static string RequireDestination(string? destPath)
    {
        if (string.IsNullOrWhiteSpace(destPath))
        {
            throw new GraphCoreException("invalidRequest", "'destPath' is required");
        }

        var directory = Path.GetDirectoryName(Path.GetFullPath(destPath));

        return string.IsNullOrEmpty(directory) || Directory.Exists(directory)
            ? destPath
            : throw new GraphCoreException(
                "invalidRequest", $"the destination directory '{directory}' does not exist");
    }

    private static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception)
        {
            // Best effort. Failing to tidy up must not replace the error being reported.
        }
    }
}
