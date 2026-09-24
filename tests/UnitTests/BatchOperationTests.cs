using System.Net;
using System.Text.Json.Nodes;
using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class BatchOperationTests
{
    /// <summary>Answers each <c>/$batch</c> POST by echoing its sub-requests back, out of order.</summary>
    private sealed class BatchTransport(Func<JsonArray, JsonArray> respond) : HttpMessageHandler
    {
        public List<int> ChunkSizes { get; } = [];

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var body = JsonNode.Parse(
                await request.Content!.ReadAsStringAsync(cancellationToken))!;
            var requests = body["requests"]!.AsArray();
            ChunkSizes.Add(requests.Count);

            var responses = respond(requests);
            var payload = new JsonObject { ["responses"] = responses }.ToJsonString();

            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(payload, System.Text.Encoding.UTF8, "application/json"),
                RequestMessage = request,
            };
        }
    }

    /// <summary>Echoes every sub-request as a success, deliberately reversed.</summary>
    private static JsonArray ReversedSuccesses(JsonArray requests)
    {
        var responses = new JsonArray();
        for (var i = requests.Count - 1; i >= 0; i--)
        {
            responses.Add(new JsonObject
            {
                ["id"] = requests[i]!["id"]!.DeepClone(),
                ["status"] = 200,
                ["body"] = new JsonObject { ["url"] = requests[i]!["url"]!.DeepClone() },
            });
        }

        return responses;
    }

    private static RequestEnvelope Batch(int count)
    {
        var requests = new JsonArray();
        for (var i = 0; i < count; i++)
        {
            requests.Add(new JsonObject { ["method"] = "GET", ["url"] = $"/users/{i}" });
        }

        return new RequestEnvelope
        {
            Method = "POST",
            Path = "/$batch",
            Body = new JsonObject { ["requests"] = requests },
        };
    }

    private static async Task<(ResponseEnvelope Response, BatchTransport Transport)> RunAsync(
        RequestEnvelope request, Func<JsonArray, JsonArray>? respond = null)
    {
        var transport = new BatchTransport(respond ?? ReversedSuccesses);
        await using var session = new GraphSession(new HttpClient(transport));

        return (await session.ExecuteAsync(request, CancellationToken.None), transport);
    }

    [Fact]
    public void A_batch_path_is_recognised_and_an_ordinary_path_is_not()
    {
        BatchOperation.Matches(Batch(1)).Should().BeTrue();
        BatchOperation.Matches(new RequestEnvelope { Method = "GET", Path = "/users" })
            .Should().BeFalse();
    }

    [Fact]
    public async Task A_25_request_batch_is_split_at_20()
    {
        var (response, transport) = await RunAsync(Batch(25));

        transport.ChunkSizes.Should().Equal(20, 5);
        response.Body!["responses"]!.AsArray().Should().HaveCount(25);
    }

    [Fact]
    public async Task Exactly_20_requests_go_as_one_chunk()
    {
        var (_, transport) = await RunAsync(Batch(20));

        transport.ChunkSizes.Should().Equal(20);
    }

    [Fact]
    public async Task Responses_come_back_in_submission_order()
    {
        // The transport deliberately reverses each chunk; input order must be restored.
        var (response, _) = await RunAsync(Batch(25));

        var urls = response.Body!["responses"]!.AsArray()
            .Select(r => r!["body"]!["url"]!.GetValue<string>())
            .ToList();

        urls.Should().Equal(Enumerable.Range(0, 25).Select(i => $"/users/{i}"));
    }

    [Fact]
    public async Task A_failing_sub_request_is_reported_in_place_rather_than_raised()
    {
        var (response, _) = await RunAsync(Batch(25), requests =>
        {
            var responses = new JsonArray();
            foreach (var sub in requests)
            {
                var url = sub!["url"]!.GetValue<string>();
                var failed = url == "/users/7";

                responses.Add(new JsonObject
                {
                    ["id"] = sub["id"]!.DeepClone(),
                    ["status"] = failed ? 404 : 200,
                    ["body"] = failed
                        ? new JsonObject
                        {
                            ["error"] = new JsonObject
                            {
                                ["code"] = "itemNotFound",
                                ["message"] = "not found",
                            },
                        }
                        : new JsonObject { ["url"] = url },
                });
            }

            return responses;
        });

        response.Ok.Should().BeTrue("one failing sub-request must not discard the other 24");

        var responses = response.Body!["responses"]!.AsArray();
        responses.Should().HaveCount(25);
        responses[7]!["status"]!.GetValue<int>().Should().Be(404);
        responses[7]!["body"]!["error"]!["code"]!.GetValue<string>().Should().Be("itemNotFound");
        responses[8]!["status"]!.GetValue<int>().Should().Be(200);
    }

    [Fact]
    public async Task Caller_supplied_ids_are_preserved_and_used_for_ordering()
    {
        var request = new RequestEnvelope
        {
            Method = "POST",
            Path = "/$batch",
            Body = JsonNode.Parse("""
                { "requests": [
                    { "id": "users", "method": "GET", "url": "/users" },
                    { "id": "groups", "method": "GET", "url": "/groups" } ] }
                """),
        };

        var (response, _) = await RunAsync(request);

        response.Body!["responses"]!.AsArray()
            .Select(r => r!["id"]!.GetValue<string>())
            .Should().Equal("users", "groups");
    }

    [Fact]
    public async Task A_batch_that_fails_outright_is_reported_as_one_failure()
    {
        var transport = RecordingTransport.Returning(
            HttpStatusCode.Forbidden,
            """{ "error": { "code": "accessDenied", "message": "no" } }""");

        await using var session = new GraphSession(new HttpClient(transport));

        var response = await session.ExecuteAsync(Batch(3), CancellationToken.None);

        response.Ok.Should().BeFalse();
        response.Status.Should().Be(403);
        response.Error!.Code.Should().Be("accessDenied");
    }

    [Fact]
    public async Task A_batch_body_without_a_requests_array_is_rejected()
    {
        var request = new RequestEnvelope
        {
            Method = "POST",
            Path = "/$batch",
            Body = JsonNode.Parse("""{ "nope": [] }"""),
        };

        var act = () => RunAsync(request);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public async Task The_batch_is_posted_to_the_v1_batch_endpoint()
    {
        var transport = new BatchTransport(ReversedSuccesses);
        await using var session = new GraphSession(new HttpClient(transport));

        await session.ExecuteAsync(Batch(1), CancellationToken.None);

        transport.ChunkSizes.Should().Equal(1);
    }
}
