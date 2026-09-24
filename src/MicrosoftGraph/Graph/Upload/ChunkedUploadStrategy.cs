using System.Net;
using System.Net.Http.Headers;
using System.Text.Json.Nodes;
using MicrosoftGraph.Models;

namespace MicrosoftGraph.Graph.Upload;

/// <summary>
/// <c>createUploadSession</c> followed by sequential ranged <c>PUT</c>s. Used at 4 MiB and above (§8.7).
/// </summary>
/// <remarks>
/// <para>
/// This is the self-contained chunk loop §8.7 named as the fallback, and it is the one that ships.
/// <c>LargeFileUploadTask</c> from Microsoft.Graph.Core cannot be used: it requires a Kiota
/// <c>IRequestAdapter</c> and is generic over the <c>IParsable</c> result types D2 deliberately
/// excludes. That verification item is resolved, and the choice stays behind
/// <see cref="IUploadStrategy"/> where no other class can see it.
/// </para>
/// </remarks>
internal sealed class ChunkedUploadStrategy(FileInfo file) : IUploadStrategy
{
    /// <summary>Graph requires an upload session's chunk size to be a multiple of 320 KiB.</summary>
    public const int ChunkAlignment = 320 * 1024;

    /// <summary>10 MiB, which is 32 whole alignment units.</summary>
    public const int ChunkSize = 10 * 1024 * 1024;

    /// <summary>Attempts per chunk before the upload gives up and reports the last response.</summary>
    private const int MaxChunkAttempts = 3;

    private const string ContentSuffix = ":/content";
    private const string SessionSuffix = ":/createUploadSession";

    public HttpRequestMessage BuildInitialRequest(GraphUrlBuilder urls, string? path, string? version) =>
        new(HttpMethod.Post, urls.Build(ToSessionPath(path), version, query: null));

    public async Task<HttpResponseMessage> SendAsync(
        HttpClient http, HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var created = await http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        if (!created.IsSuccessStatusCode)
        {
            // Let the shared error path report why the session could not be created.
            return created;
        }

        var uploadUrl = await ReadUploadUrlAsync(created, cancellationToken).ConfigureAwait(false);
        created.Dispose();

        return await SendChunksAsync(http, uploadUrl, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// Turns the content path the caller wrote into the session path Graph expects, so the two
    /// strategies take the same input and the caller never has to know which one ran.
    /// </summary>
    internal static string ToSessionPath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            throw new GraphCoreException("invalidRequest", "'path' is required");
        }

        if (path.EndsWith(ContentSuffix, StringComparison.Ordinal))
        {
            return string.Concat(path.AsSpan(0, path.Length - ContentSuffix.Length), SessionSuffix);
        }

        return path.EndsWith("/content", StringComparison.Ordinal)
            ? string.Concat(path.AsSpan(0, path.Length - "/content".Length), "/createUploadSession")
            : path;
    }

    private async Task<HttpResponseMessage> SendChunksAsync(
        HttpClient http, Uri uploadUrl, CancellationToken cancellationToken)
    {
        await using var source = file.OpenRead();
        var buffer = new byte[ChunkSize];

        var offset = 0L;
        HttpResponseMessage? last = null;

        while (offset < file.Length)
        {
            var read = await ReadChunkAsync(source, buffer, offset, cancellationToken).ConfigureAwait(false);

            last?.Dispose();
            last = await PutChunkAsync(http, uploadUrl, buffer, offset, read, cancellationToken)
                .ConfigureAwait(false);

            if (!last.IsSuccessStatusCode)
            {
                // Out of attempts for this chunk; the shared error path reports the last response.
                return last;
            }

            offset += read;

            // The service may have accepted less than was sent, or already hold more than was
            // sent if an earlier attempt landed. Its own account of progress wins.
            var resumeFrom = await ReadNextExpectedStartAsync(last, cancellationToken).ConfigureAwait(false);
            if (resumeFrom is { } next && next != offset && next < file.Length)
            {
                offset = next;
            }
        }

        return last ?? throw new GraphCoreException("invalidRequest", "the file is empty");
    }

    private async Task<HttpResponseMessage> PutChunkAsync(
        HttpClient http,
        Uri uploadUrl,
        byte[] buffer,
        long offset,
        int length,
        CancellationToken cancellationToken)
    {
        HttpResponseMessage? response = null;

        for (var attempt = 1; attempt <= MaxChunkAttempts; attempt++)
        {
            response?.Dispose();

            using var request = new HttpRequestMessage(HttpMethod.Put, uploadUrl)
            {
                Content = new ByteArrayContent(buffer, 0, length),
            };

            request.Content.Headers.ContentLength = length;
            request.Content.Headers.ContentRange =
                new ContentRangeHeaderValue(offset, offset + length - 1, file.Length);

            response = await http.SendAsync(request, cancellationToken).ConfigureAwait(false);

            if (response.IsSuccessStatusCode || !IsTransient(response.StatusCode))
            {
                return response;
            }
        }

        return response!;
    }

    private static async Task<int> ReadChunkAsync(
        Stream source, byte[] buffer, long offset, CancellationToken cancellationToken)
    {
        source.Seek(offset, SeekOrigin.Begin);

        var read = 0;
        while (read < buffer.Length)
        {
            var got = await source
                .ReadAsync(buffer.AsMemory(read, buffer.Length - read), cancellationToken)
                .ConfigureAwait(false);

            if (got == 0)
            {
                break;
            }

            read += got;
        }

        return read;
    }

    private static async Task<Uri> ReadUploadUrlAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var body = await ParseAsync(response, cancellationToken).ConfigureAwait(false);
        var uploadUrl = body?["uploadUrl"]?.GetValue<string>();

        return Uri.TryCreate(uploadUrl, UriKind.Absolute, out var parsed)
            ? parsed
            : throw new GraphCoreException(
                "internalError", "the upload session response carried no uploadUrl");
    }

    /// <summary>
    /// Reads the session's <c>nextExpectedRanges</c>, which is what makes a large upload resumable
    /// within a single call.
    /// </summary>
    private static async Task<long?> ReadNextExpectedStartAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var ranges = (await ParseAsync(response, cancellationToken).ConfigureAwait(false))
            ?["nextExpectedRanges"]?.AsArray();

        var first = ranges is { Count: > 0 } ? ranges[0]?.GetValue<string>() : null;
        var start = first?.Split('-', 2)[0];

        return long.TryParse(start, out var parsed) ? parsed : null;
    }

    private static async Task<JsonNode?> ParseAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var raw = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);

        try
        {
            return string.IsNullOrWhiteSpace(raw) ? null : JsonNode.Parse(raw);
        }
        catch (System.Text.Json.JsonException)
        {
            return null;
        }
    }

    private static bool IsTransient(HttpStatusCode status) =>
        status is HttpStatusCode.RequestTimeout
            or HttpStatusCode.TooManyRequests
            or HttpStatusCode.InternalServerError
            or HttpStatusCode.BadGateway
            or HttpStatusCode.ServiceUnavailable
            or HttpStatusCode.GatewayTimeout;
}
