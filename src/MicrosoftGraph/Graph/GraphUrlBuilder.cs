using System.Text;
using System.Text.Json;
using MicrosoftGraph.Models;

namespace MicrosoftGraph.Graph;

/// <summary>Builds Graph request URLs (§8.3).</summary>
internal sealed class GraphUrlBuilder
{
    public const string DefaultVersion = "v1.0";

    private const string BaseUrl = "https://graph.microsoft.com";

    private static readonly string[] SupportedVersions = [DefaultVersion, "beta"];

    /// <summary>
    /// An absolute <paramref name="path"/> is used verbatim and <paramref name="version"/> and
    /// <paramref name="query"/> are ignored, because the URL already carries them. That is what
    /// makes <c>nextLink</c> echo-back and pre-authenticated download URLs work with no special case.
    /// </summary>
    /// <remarks>
    /// Absolute URLs may point off <c>graph.microsoft.com</c> (download URLs do). The bearer token
    /// is withheld from those by the auth provider's allowed-hosts validator, not by this method.
    /// </remarks>
    public Uri Build(string? path, string? version, IReadOnlyDictionary<string, JsonElement>? query)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            throw new GraphCoreException("invalidRequest", "'path' is required");
        }

        if (Uri.TryCreate(path, UriKind.Absolute, out var absolute))
        {
            return absolute.Scheme == Uri.UriSchemeHttps
                ? absolute
                : throw new GraphCoreException("invalidRequest", "an absolute 'path' must use https");
        }

        var url = new StringBuilder(BaseUrl)
            .Append('/')
            .Append(ResolveVersion(version))
            .Append(path.StartsWith('/') ? path : "/" + path);

        AppendQuery(url, query);
        return new Uri(url.ToString(), UriKind.Absolute);
    }

    private static string ResolveVersion(string? version)
    {
        if (string.IsNullOrWhiteSpace(version))
        {
            return DefaultVersion;
        }

        return SupportedVersions.Contains(version)
            ? version
            : throw new GraphCoreException(
                "invalidRequest", $"'version' must be one of {string.Join(", ", SupportedVersions)}");
    }

    /// <summary>
    /// Escapes names as well as values. <c>ParametersNameDecodingHandler</c> in the pipeline decodes
    /// <c>%24</c> back to <c>$</c>, so callers write <c>$select</c> and never pre-encode anything.
    /// </summary>
    private static void AppendQuery(StringBuilder url, IReadOnlyDictionary<string, JsonElement>? query)
    {
        if (query is null || query.Count == 0)
        {
            return;
        }

        var separator = '?';
        foreach (var (name, value) in query)
        {
            url.Append(separator)
               .Append(Uri.EscapeDataString(name))
               .Append('=')
               .Append(Uri.EscapeDataString(ToQueryValue(value)));
            separator = '&';
        }
    }

    private static string ToQueryValue(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => value.GetString() ?? string.Empty,
        JsonValueKind.Null or JsonValueKind.Undefined => string.Empty,
        _ => value.GetRawText(),
    };
}
