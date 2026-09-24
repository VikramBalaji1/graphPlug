namespace MicrosoftGraph.Models;

/// <summary>
/// Response headers reach the caller through an allowlist, never a denylist (§6.7): a denylist
/// fails open on whatever header Microsoft adds tomorrow. <c>Authorization</c> and
/// <c>WWW-Authenticate</c> therefore cannot escape even by accident.
/// </summary>
internal static class ResponseHeaderFilter
{
    private static readonly string[] Allowlist =
        ["request-id", "client-request-id", "Date", "Retry-After", "Content-Type", "Location", "ETag"];

    public static Dictionary<string, string> Apply(HttpResponseMessage response)
    {
        var collected = NewCollection();
        foreach (var name in Allowlist)
        {
            if (response.Headers.TryGetValues(name, out var values) ||
                response.Content.Headers.TryGetValues(name, out values))
            {
                collected[name] = string.Join(", ", values);
            }
        }

        return collected;
    }

    /// <summary>For headers recovered from a Kiota <c>ApiException</c> rather than a live response.</summary>
    public static Dictionary<string, string> Apply(IDictionary<string, IEnumerable<string>>? headers)
    {
        var collected = NewCollection();
        if (headers is null)
        {
            return collected;
        }

        // Iterated rather than looked up, because this dictionary need not be case-insensitive.
        foreach (var (name, values) in headers)
        {
            var allowed = Array.Find(Allowlist, a => a.Equals(name, StringComparison.OrdinalIgnoreCase));
            if (allowed is not null)
            {
                collected[allowed] = string.Join(", ", values);
            }
        }

        return collected;
    }

    private static Dictionary<string, string> NewCollection() => new(StringComparer.OrdinalIgnoreCase);
}
