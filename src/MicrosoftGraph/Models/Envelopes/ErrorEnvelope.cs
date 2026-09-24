using System.Text.Json;
using Azure.Identity;
using Microsoft.Kiota.Abstractions;

namespace MicrosoftGraph.Models.Envelopes;

/// <summary>
/// The failure envelope emitted by the blanket catch at every export (§6.2). Same shape as a
/// failed <see cref="ResponseEnvelope"/>, so Python has exactly one error shape to handle.
/// </summary>
internal sealed record ErrorEnvelope
{
    public bool Ok => false;

    /// <summary>Zero for failures with no HTTP response behind them (§9.2).</summary>
    public int Status { get; init; }

    /// <summary>
    /// Reported even on a failure, so the binding can verify the pair at load time with one
    /// deliberately rejected call rather than waiting for the first real session (§6.6).
    /// </summary>
    public string CoreVersion => Models.CoreVersion.Value;

    public required GraphErrorInfo Error { get; init; }

    public static ErrorEnvelope From(Exception exception)
    {
        var cause = Unwrap(exception);

        // Kiota's RetryHandler throws once its budget is exhausted rather than returning the last
        // response, so a throttled request would otherwise surface as `internalError` with no
        // status and no Retry-After. Recovering both keeps §8.1's promise that an exhausted budget
        // is visible to the caller.
        return cause is ApiException api
            ? FromApiException(api)
            : new ErrorEnvelope
            {
                Error = new GraphErrorInfo
                {
                    Code = CodeFor(cause),

                    // Flattened, because Azure.Identity puts the AADSTS detail on an inner
                    // exception and the outer message is often an empty prefix.
                    Message = cause is AuthenticationFailedException
                        ? EntraFailure.Flatten(cause)
                        : cause.Message,
                },
            };
    }

    private static ErrorEnvelope FromApiException(ApiException api)
    {
        var headers = ResponseHeaderFilter.Apply(api.ResponseHeaders);

        return new ErrorEnvelope
        {
            Status = api.ResponseStatusCode,
            Error = GraphErrorInfo.From(
                fallbackCode: "requestFailed",
                allowlistedHeaders: headers,
                rawBody: TrailingJson(api.Message),
                fallbackMessage: api.Message),
        };
    }

    /// <summary>
    /// Kiota appends the response body to the exception message. Recovering it restores Graph's own
    /// error code and inner error; if the shape ever changes, the fallbacks above still apply.
    /// </summary>
    private static string TrailingJson(string message)
    {
        var start = message.IndexOf('{');
        return start >= 0 ? message[start..] : string.Empty;
    }

    private static Exception Unwrap(Exception exception) => exception switch
    {
        // The last attempt is the one the caller needs to see.
        AggregateException aggregate when aggregate.Flatten().InnerExceptions.Count > 0 =>
            aggregate.Flatten().InnerExceptions[^1],
        _ => exception,
    };

    private static string CodeFor(Exception exception) => exception switch
    {
        GraphCoreException core => core.Code,
        AuthenticationFailedException => EntraFailure.CodeFor(exception),
        OperationCanceledException => "timeout",
        HttpRequestException => "transportError",
        JsonException => "invalidRequest",
        _ => "internalError",
    };
}
