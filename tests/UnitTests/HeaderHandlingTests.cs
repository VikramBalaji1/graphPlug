using System.Net;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class HeaderHandlingTests
{
    [Fact]
    public async Task Forwards_caller_supplied_request_headers()
    {
        var transport = StubHttpMessageHandler.Returning(HttpStatusCode.OK, """{ "value": [] }""");

        await OperationRunner.RunAsync(
            new RequestEnvelope
            {
                Method = "GET",
                Path = "/users",
                Headers = new Dictionary<string, string> { ["ConsistencyLevel"] = "eventual" },
            },
            transport);

        transport.RequestHeaders.Should().Contain(
            new KeyValuePair<string, string>("ConsistencyLevel", "eventual"));
    }

    [Theory]
    [InlineData("Authorization")]
    [InlineData("authorization")]
    [InlineData("AUTHORIZATION")]
    public async Task Rejects_a_caller_supplied_Authorization_header(string name)
    {
        var transport = StubHttpMessageHandler.Returning(HttpStatusCode.OK, "{}");

        var act = () => OperationRunner.RunAsync(
            new RequestEnvelope
            {
                Method = "GET",
                Path = "/users",
                Headers = new Dictionary<string, string> { [name] = "Bearer leaked-token" },
            },
            transport);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("invalidRequest");

        transport.RequestUri.Should().BeNull("the request must never reach the wire");
    }

    [Fact]
    public async Task Returns_only_allowlisted_response_headers()
    {
        var transport = StubHttpMessageHandler.Returning(
            HttpStatusCode.OK,
            "{}",
            ("request-id", "rid-1"),
            ("client-request-id", "crid-1"),
            ("ETag", "W/\"1\""),
            ("Authorization", "Bearer should-never-escape"),
            ("WWW-Authenticate", "Bearer realm=\"graph\""),
            ("x-ms-ags-diagnostic", "internal"));

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport);

        response.Headers.Should().ContainKeys("request-id", "client-request-id", "ETag", "Content-Type");
        response.Headers.Should().NotContainKeys("Authorization", "WWW-Authenticate", "x-ms-ags-diagnostic");
    }

    [Fact]
    public async Task Never_serialises_credential_material_into_the_envelope()
    {
        var transport = StubHttpMessageHandler.Returning(
            HttpStatusCode.Unauthorized,
            """{ "error": { "code": "InvalidAuthenticationToken", "message": "Access token is empty." } }""",
            ("WWW-Authenticate", "Bearer realm=\"\", error=\"invalid_token\""),
            ("Authorization", "Bearer super-secret"));

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/me" }, transport);

        var json = System.Text.Json.JsonSerializer.Serialize(
            response, GraphJsonContext.Default.ResponseEnvelope);

        json.Should().NotContain("super-secret").And.NotContain("Authorization");
    }
}
