using System.Net;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class PaginationTests
{
    private const string NextLink = "https://graph.microsoft.com/v1.0/users?$skiptoken=X-Y_Z";

    [Fact]
    public async Task Lifts_odata_nextLink_to_the_top_level()
    {
        var transport = StubHttpMessageHandler.Returning(HttpStatusCode.OK, $$"""
            { "value": [ { "id": "1" } ], "@odata.nextLink": "{{NextLink}}" }
            """);

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport);

        response.NextLink.Should().Be(NextLink);
    }

    [Fact]
    public async Task Omits_nextLink_on_the_last_page()
    {
        var transport = StubHttpMessageHandler.Returning(
            HttpStatusCode.OK, """{ "value": [ { "id": "1" } ] }""");

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport);

        response.NextLink.Should().BeNull();
    }

    [Fact]
    public async Task Leaves_odata_count_and_deltaLink_untouched_in_the_body()
    {
        var transport = StubHttpMessageHandler.Returning(HttpStatusCode.OK, """
            { "value": [], "@odata.count": 42, "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d" }
            """);

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users/delta" }, transport);

        response.Body!["@odata.count"]!.GetValue<int>().Should().Be(42);
        response.Body["@odata.deltaLink"].Should().NotBeNull();
    }

    [Fact]
    public async Task Echoing_a_nextLink_back_as_the_path_reaches_the_same_url()
    {
        var transport = StubHttpMessageHandler.Returning(HttpStatusCode.OK, """{ "value": [] }""");

        await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = NextLink }, transport);

        transport.RequestUri!.OriginalString.Should().Be(NextLink);
    }

    [Fact]
    public async Task Returns_a_null_body_for_204_no_content()
    {
        var transport = StubHttpMessageHandler.Returning(HttpStatusCode.NoContent);

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "DELETE", Path = "/users/1" }, transport);

        response.Ok.Should().BeTrue();
        response.Status.Should().Be(204);
        response.Body.Should().BeNull();
        response.NextLink.Should().BeNull();
    }
}
