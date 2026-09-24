using System.Text.Json;
using MicrosoftGraph.Graph;
using MicrosoftGraph.Models;

namespace UnitTests;

public class GraphUrlBuilderTests
{
    private readonly GraphUrlBuilder _urls = new();

    /// <summary>Built without reflection, because the test host runs with it disabled (§10).</summary>
    private static Dictionary<string, JsonElement> Query(string json)
    {
        using var document = JsonDocument.Parse(json);
        return document.RootElement.EnumerateObject()
            .ToDictionary(property => property.Name, property => property.Value.Clone());
    }

    [Fact]
    public void Defaults_to_v1_when_no_version_is_given()
    {
        _urls.Build("/users", null, null)
            .Should().Be(new Uri("https://graph.microsoft.com/v1.0/users"));
    }

    [Fact]
    public void Uses_the_beta_endpoint_when_asked_explicitly()
    {
        _urls.Build("/users", "beta", null)
            .Should().Be(new Uri("https://graph.microsoft.com/beta/users"));
    }

    [Fact]
    public void Adds_a_leading_slash_to_a_relative_path()
    {
        _urls.Build("users", null, null)
            .Should().Be(new Uri("https://graph.microsoft.com/v1.0/users"));
    }

    [Fact]
    public void Rejects_an_unknown_api_version()
    {
        _urls.Invoking(u => u.Build("/users", "v2.0", null))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public void Requires_a_path()
    {
        _urls.Invoking(u => u.Build(null, null, null))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public void Encodes_odata_parameter_names_and_values()
    {
        var url = _urls.Build("/users", null, Query("""
            { "$select": "id,displayName", "$top": 999 }
            """));

        // Names are escaped too; ParametersNameDecodingHandler decodes %24 back to $ in the pipeline.
        url.OriginalString.Should().Be(
            "https://graph.microsoft.com/v1.0/users?%24select=id%2CdisplayName&%24top=999");
    }

    [Fact]
    public void Encodes_a_filter_value_containing_spaces_and_quotes()
    {
        var url = _urls.Build("/users", null, Query("""
            { "$filter": "startsWith(displayName,'a b')" }
            """));

        url.OriginalString.Should().Contain("startsWith%28displayName%2C%27a%20b%27%29");
    }

    [Fact]
    public void Passes_an_absolute_url_through_verbatim_ignoring_version_and_query()
    {
        const string NextLink = "https://graph.microsoft.com/v1.0/users?$skiptoken=X-Y_Z";

        _urls.Build(NextLink, "beta", Query("""{ "$top": 5 }"""))
            .OriginalString.Should().Be(NextLink);
    }

    [Fact]
    public void Passes_an_off_host_absolute_url_through_for_downloads()
    {
        // Pre-authenticated download URLs live off graph.microsoft.com. They are allowed through;
        // the auth provider's allowed-hosts validator is what withholds the bearer token from them.
        const string DownloadUrl = "https://contoso.sharepoint.com/_layouts/download.aspx?id=1";

        _urls.Build(DownloadUrl, null, null).OriginalString.Should().Be(DownloadUrl);
    }

    [Fact]
    public void Rejects_a_non_https_absolute_url()
    {
        _urls.Invoking(u => u.Build("http://graph.microsoft.com/v1.0/users", null, null))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }
}
