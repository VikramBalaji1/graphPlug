using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MicrosoftGraph.Models;

/// <summary>The <c>error</c> object of the failure envelope (§9.1).</summary>
/// <remarks>Every property is optional except <c>code</c> and <c>message</c>; absent fields are
/// omitted from the JSON rather than written as null.</remarks>
internal sealed record GraphErrorInfo
{
    private const int MaxErrorBodyChars = 2048;

    public required string Code { get; init; }

    public required string Message { get; init; }

    public string? RequestId { get; init; }

    public string? ClientRequestId { get; init; }

    public string? Date { get; init; }

    public int? RetryAfterSeconds { get; init; }

    public JsonNode? InnerError { get; init; }

    /// <summary>
    /// The single place a Graph failure becomes an error object. Both a live response and a
    /// failure recovered from a Kiota <c>ApiException</c> come through here, which is what makes
    /// the consistency §9 promises structural rather than a convention someone has to remember.
    /// </summary>
    public static GraphErrorInfo From(
        string fallbackCode,
        IReadOnlyDictionary<string, string> allowlistedHeaders,
        string rawBody,
        string? fallbackMessage = null)
    {
        var error = TryParse(rawBody)?["error"];

        return new GraphErrorInfo
        {
            Code = AsString(error?["code"]) ?? fallbackCode,
            Message = AsString(error?["message"])
                ?? fallbackMessage
                ?? (rawBody.Length > 0 ? rawBody[..Math.Min(rawBody.Length, MaxErrorBodyChars)] : fallbackCode),
            RequestId = allowlistedHeaders.GetValueOrDefault("request-id"),
            ClientRequestId = allowlistedHeaders.GetValueOrDefault("client-request-id"),
            Date = allowlistedHeaders.GetValueOrDefault("Date"),
            RetryAfterSeconds = ParseRetryAfter(allowlistedHeaders.GetValueOrDefault("Retry-After")),

            // Graph's inner error is frequently the only actionable part of a failure, so it is
            // preserved verbatim rather than flattened into fields the core picked out.
            InnerError = error?["innerError"]?.DeepClone(),
        };
    }

    /// <summary>Accepts both forms RFC 9110 allows: delta-seconds and an HTTP date.</summary>
    private static int? ParseRetryAfter(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return null;
        }

        if (int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var seconds))
        {
            return Math.Max(0, seconds);
        }

        return DateTimeOffset.TryParseExact(
            value, "R", CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var when)
            ? Math.Max(0, (int)Math.Ceiling((when - DateTimeOffset.UtcNow).TotalSeconds))
            : null;
    }

    private static JsonNode? TryParse(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
        {
            return null;
        }

        try
        {
            return JsonNode.Parse(raw);
        }
        catch (JsonException)
        {
            // Not every Graph failure body is JSON — a gateway can return HTML.
            return null;
        }
    }

    private static string? AsString(JsonNode? node) => node?.GetValueKind() switch
    {
        JsonValueKind.String => node.GetValue<string>(),
        null => null,
        _ => node.ToJsonString(),
    };
}
