using System.Globalization;
using System.Text.Json;

namespace MicrosoftGraph.Diagnostics;

internal enum GraphLogLevel
{
    Off = 0,
    Error = 1,
    Info = 2,
}

/// <summary>
/// Minimal structured logging for cross-call correlation (§14). One JSON object per line on
/// stderr, off unless <c>MSGRAPH_LOG_LEVEL</c> asks for it.
/// </summary>
/// <remarks>
/// <para>
/// No logging dependency, no DI container and no ABI change: a shared library loaded by
/// <c>ctypes</c> has no host to configure a logger, and NativeAOT penalises the reflection one
/// would bring (§4, §10). Output goes to stderr because a Python caller may be piping stdout.
/// </para>
/// <para>
/// Redaction is structural, not a rule to remember. Every method takes the exact fields it may
/// emit, so there is no free-form overload through which a header, a token or a request body
/// could reach a log line. URLs are logged without their query string, because an OData filter
/// routinely carries user identifiers.
/// </para>
/// </remarks>
internal static class GraphLog
{
    public const string LevelVariable = "MSGRAPH_LOG_LEVEL";

    /// <summary>Read once at startup; a library should not pay a lookup per request.</summary>
    internal static GraphLogLevel Level { get; set; } =
        ParseLevel(Environment.GetEnvironmentVariable(LevelVariable));

    /// <summary>Overridable so tests can capture output without touching the console.</summary>
    internal static TextWriter Writer { get; set; } = Console.Error;

    public static bool IsEnabled(GraphLogLevel level) => level <= Level;

    public static void RequestCompleted(
        string method, Uri uri, int status, TimeSpan elapsed, string? requestId, string? errorCode)
    {
        var level = errorCode is null ? GraphLogLevel.Info : GraphLogLevel.Error;
        if (!IsEnabled(level))
        {
            return;
        }

        Write(
            level,
            $"\"event\":\"request\",\"method\":{Quote(method)},\"url\":{Quote(Sanitise(uri))}," +
            $"\"status\":{status},\"ms\":{Milliseconds(elapsed)}," +
            $"\"requestId\":{Quote(requestId)},\"errorCode\":{Quote(errorCode)}");
    }

    public static void SessionEvent(string name, long handle)
    {
        if (!IsEnabled(GraphLogLevel.Info))
        {
            return;
        }

        Write(GraphLogLevel.Info, $"\"event\":{Quote(name)},\"handle\":{handle}");
    }

    /// <summary>
    /// A failure at the ABI boundary. The message is safe to log because it is the same text
    /// already returned to the caller in the error envelope (§9.2) — logging adds no exposure.
    /// </summary>
    public static void BoundaryFailure(string entryPoint, string code, string message)
    {
        if (!IsEnabled(GraphLogLevel.Error))
        {
            return;
        }

        Write(
            GraphLogLevel.Error,
            $"\"event\":\"failure\",\"entryPoint\":{Quote(entryPoint)}," +
            $"\"code\":{Quote(code)},\"message\":{Quote(message)}");
    }

    /// <summary>Scheme, host and path only — the query string is never logged.</summary>
    private static string Sanitise(Uri uri) => uri.GetLeftPart(UriPartial.Path);

    private static string Milliseconds(TimeSpan elapsed) =>
        ((long)elapsed.TotalMilliseconds).ToString(CultureInfo.InvariantCulture);

    private static string Quote(string? value) =>
        value is null ? "null" : $"\"{JsonEncodedText.Encode(value)}\"";

    private static void Write(GraphLogLevel level, string fields)
    {
        try
        {
            Writer.WriteLine($"{{\"level\":\"{Name(level)}\",{fields}}}");
        }
        catch (Exception)
        {
            // A diagnostic must never be the thing that takes the process down (§6.2).
        }
    }

    private static string Name(GraphLogLevel level) => level == GraphLogLevel.Error ? "error" : "info";

    /// <summary>Anything unrecognised means off: a typo must not silently enable logging.</summary>
    internal static GraphLogLevel ParseLevel(string? value) =>
        value?.Trim().ToLowerInvariant() switch
        {
            "error" => GraphLogLevel.Error,
            "info" => GraphLogLevel.Info,
            _ => GraphLogLevel.Off,
        };
}
