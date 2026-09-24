using System.Text;

namespace MicrosoftGraph.Models;

/// <summary>
/// Works out what Entra actually said, and which of §9.2's codes it corresponds to.
/// </summary>
/// <remarks>
/// The detail is never on the outermost exception. Azure.Identity throws
/// <c>AuthenticationFailedException</c> with a message like "DeviceCodeCredential authentication
/// failed: " and puts MSAL's real error — the AADSTS number — on the inner exception. Reading only
/// the top message silently degrades every delegated failure to <c>authenticationFailed</c>.
/// </remarks>
internal static class EntraFailure
{
    private const int MaxDepth = 8;

    public static string CodeFor(Exception exception) => Classify(Flatten(exception));

    /// <summary>
    /// Every message in the chain, so the caller sees the AADSTS detail rather than an empty
    /// prefix. Azure.Identity is careful not to put credential material in these (§9.2).
    /// </summary>
    public static string Flatten(Exception exception)
    {
        var text = new StringBuilder();
        Append(exception, text, 0);

        return text.Length > 0 ? text.ToString() : exception.GetType().Name;
    }

    private static void Append(Exception? exception, StringBuilder text, int depth)
    {
        if (exception is null || depth >= MaxDepth)
        {
            return;
        }

        if (!string.IsNullOrWhiteSpace(exception.Message))
        {
            if (text.Length > 0)
            {
                text.Append(" — ");
            }

            text.Append(exception.Message.Trim());
        }

        if (exception is AggregateException aggregate)
        {
            foreach (var inner in aggregate.Flatten().InnerExceptions)
            {
                Append(inner, text, depth + 1);
            }

            return;
        }

        Append(exception.InnerException, text, depth + 1);
    }

    /// <summary>
    /// Matched on AADSTS numbers and OAuth error codes rather than prose, which is localised and
    /// reworded over time.
    /// </summary>
    private static string Classify(string message)
    {
        // 65004 is an active refusal; 65001 is merely the absence of a consent grant.
        if (message.Contains("AADSTS65004"))
        {
            return "signInDeclined";
        }

        if (message.Contains("AADSTS65001") || message.Contains("consent_required"))
        {
            return "consentRequired";
        }

        if (message.Contains("AADSTS70016") || message.Contains("expired_token") ||
            message.Contains("code_expired"))
        {
            return "signInTimeout";
        }

        return message.Contains("authorization_declined") || message.Contains("access_denied")
            ? "signInDeclined"
            : "authenticationFailed";
    }
}
