using System.Diagnostics;
using System.Net;
using MicrosoftGraph.Diagnostics;
using System.Text.Json.Nodes;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Graph.Operations;

/// <summary>
/// The shared request pipeline (§8.2): build a URL, apply headers, send, map the response.
/// Subclasses fill in only what differs. Error mapping is inherited, which is what makes a
/// download failure and a batch failure produce the same envelope as an ordinary request failure.
/// </summary>
internal abstract class GraphOperation(IReadOnlyDictionary<string, string>? headers)
{
    public async Task<ResponseEnvelope> ExecuteAsync(
        GraphSession session, CancellationToken cancellationToken)
    {
        using var request = BuildRequest(session.Urls);
        ApplyRequestHeaders(request);

        var startedAt = Stopwatch.GetTimestamp();

        using var response = await SendAsync(session.Http, request, cancellationToken)
            .ConfigureAwait(false);

        var envelope = response.IsSuccessStatusCode
            ? await MapSuccessAsync(response, cancellationToken).ConfigureAwait(false)
            : await MapErrorAsync(response, cancellationToken).ConfigureAwait(false);

        // Logged here rather than per subclass, so every operation type is covered by construction.
        // A request that throws instead of returning is logged by the export that catches it.
        GraphLog.RequestCompleted(
            request.Method.Method,
            request.RequestUri!,
            envelope.Status,
            Stopwatch.GetElapsedTime(startedAt),
            envelope.Error?.RequestId ?? envelope.Headers?.GetValueOrDefault("request-id"),
            envelope.Error?.Code);

        return envelope;
    }

    protected abstract HttpRequestMessage BuildRequest(GraphUrlBuilder urls);

    protected abstract Task<ResponseEnvelope> MapSuccessAsync(
        HttpResponseMessage response, CancellationToken cancellationToken);

    protected virtual Task<HttpResponseMessage> SendAsync(
        HttpClient http, HttpRequestMessage request, CancellationToken cancellationToken) =>
        http.SendAsync(request, cancellationToken);

    /// <summary>
    /// Forwards caller headers as supplied, except <c>Authorization</c>. The core owns
    /// authentication; a caller-supplied bearer token would silently bypass the handle's credential.
    /// </summary>
    private void ApplyRequestHeaders(HttpRequestMessage request)
    {
        if (headers is null)
        {
            return;
        }

        foreach (var (name, value) in headers)
        {
            if (string.Equals(name, "Authorization", StringComparison.OrdinalIgnoreCase))
            {
                throw new GraphCoreException(
                    "invalidRequest",
                    "'Authorization' may not be supplied; the core owns authentication");
            }

            if (!request.Headers.TryAddWithoutValidation(name, value))
            {
                request.Content?.Headers.TryAddWithoutValidation(name, value);
            }
        }
    }

    protected async Task<ResponseEnvelope> MapErrorAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var headers = ResponseHeaderFilter.Apply(response);
        var raw = await ReadBodyTextAsync(response, cancellationToken).ConfigureAwait(false);

        return new ResponseEnvelope
        {
            Ok = false,
            Status = (int)response.StatusCode,
            Headers = headers,
            Error = GraphErrorInfo.From(
                fallbackCode: response.StatusCode.ToString(),
                allowlistedHeaders: headers,
                rawBody: raw,
                fallbackMessage: raw.Length > 0 ? null : response.ReasonPhrase),
        };
    }

    protected static async Task<JsonNode?> ReadJsonBodyAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        if (response.StatusCode == HttpStatusCode.NoContent)
        {
            return null;
        }

        var raw = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        return string.IsNullOrWhiteSpace(raw) ? null : JsonNode.Parse(raw);
    }

    private static async Task<string> ReadBodyTextAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        try
        {
            return await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (Exception) when (!cancellationToken.IsCancellationRequested)
        {
            // An unreadable error body must not replace the status code we already have.
            return string.Empty;
        }
    }
}
