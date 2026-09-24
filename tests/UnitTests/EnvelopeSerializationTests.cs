using System.Text.Json;
using System.Text.Json.Nodes;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

/// <summary>
/// Reflection-based serialisation is switched off (§10), so these round-trips also prove that
/// every ABI type is reachable through the source-generated context.
/// </summary>
public class EnvelopeSerializationTests
{
    [Fact]
    public void Credentials_envelope_round_trips()
    {
        const string Json = """
            { "type": "clientSecret", "tenantId": "t", "clientId": "c", "clientSecret": "s",
              "scopes": ["https://graph.microsoft.com/.default"], "authorityHost": "https://login.microsoftonline.com",
              "retry": { "maxRetries": 5, "maxDelaySeconds": 60 } }
            """;

        var credentials = JsonSerializer.Deserialize(Json, GraphJsonContext.Default.CredentialsEnvelope)!;

        credentials.Type.Should().Be("clientSecret");
        credentials.Scopes.Should().Equal("https://graph.microsoft.com/.default");
        credentials.Retry!.MaxRetries.Should().Be(5);
        credentials.Retry.MaxDelaySeconds.Should().Be(60);
    }

    [Fact]
    public void Request_envelope_round_trips_including_mixed_type_query_values()
    {
        const string Json = """
            { "method": "GET", "path": "/users", "version": "beta",
              "query": { "$select": "id", "$top": 999 },
              "headers": { "ConsistencyLevel": "eventual" },
              "body": { "displayName": "A" }, "timeoutMs": 5000 }
            """;

        var request = JsonSerializer.Deserialize(Json, GraphJsonContext.Default.RequestEnvelope)!;

        request.Method.Should().Be("GET");
        request.Version.Should().Be("beta");
        request.Query!["$top"].GetInt32().Should().Be(999);
        request.Headers!["ConsistencyLevel"].Should().Be("eventual");
        request.Body!["displayName"]!.GetValue<string>().Should().Be("A");
        request.TimeoutMs.Should().Be(5000);
    }

    [Fact]
    public void Response_envelope_serialises_with_camel_case_names_and_no_null_fields()
    {
        var response = new ResponseEnvelope
        {
            Ok = true,
            Status = 200,
            Headers = new Dictionary<string, string> { ["request-id"] = "r" },
            Body = JsonNode.Parse("""{ "value": [] }"""),
            NextLink = "https://graph.microsoft.com/v1.0/users?$skiptoken=1",
        };

        var json = JsonSerializer.Serialize(response, GraphJsonContext.Default.ResponseEnvelope);

        json.Should().Contain("\"ok\":true")
            .And.Contain("\"nextLink\":")
            .And.NotContain("\"error\"");
    }

    [Fact]
    public void Error_envelope_serialises_as_ok_false_with_status_zero()
    {
        var json = JsonSerializer.Serialize(
            ErrorEnvelope.From(new GraphCoreException("invalidHandle", "handle 7 is unknown or closed")),
            GraphJsonContext.Default.ErrorEnvelope);

        json.Should().Contain("\"ok\":false")
            .And.Contain("\"status\":0")
            .And.Contain("\"code\":\"invalidHandle\"");
    }

    [Fact]
    public void Session_envelope_carries_the_core_version()
    {
        var json = JsonSerializer.Serialize(
            new SessionEnvelope { Ok = true, Handle = 1, CoreVersion = CoreVersion.Value },
            GraphJsonContext.Default.SessionEnvelope);

        json.Should().Contain("\"handle\":1").And.Contain($"\"coreVersion\":\"{CoreVersion.Value}\"");
    }
}
