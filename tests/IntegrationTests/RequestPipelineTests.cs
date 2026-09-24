using System.Diagnostics;
using System.Net;
using System.Text.Json;
using MicrosoftGraph.Authentication.AppOnly;
using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;
using static IntegrationTests.ScriptedTransport;

namespace IntegrationTests;

/// <summary>
/// The full Microsoft handler pipeline over a controlled transport (§13). These are what prove the
/// retry, throttling and re-auth behaviour the package deliberately does not implement itself.
/// </summary>
public class RequestPipelineTests
{
    private static readonly string[] DefaultScope = [ClientSecretStrategy.DefaultScope];

    private static async Task<ResponseEnvelope> RequestAsync(
        RequestEnvelope request,
        ScriptedTransport transport,
        FakeTokenCredential credential,
        RetrySettings? retry = null)
    {
        await using var session = GraphSession.Create(
            credential, DefaultScope, retry, finalHandler: transport);

        return await session.Executor.ExecuteAsync(
            new JsonRequestOperation(request), CancellationToken.None);
    }

    [Fact]
    public async Task Round_trips_a_request_through_the_whole_pipeline()
    {
        var transport = new ScriptedTransport(
            Response(HttpStatusCode.OK, """{ "value": [ { "id": "1" } ] }""", ("request-id", "rid")));

        var response = await RequestAsync(
            new RequestEnvelope
            {
                Method = "GET",
                Path = "/users",
                Query = Query("""{ "$select": "id" }"""),
            },
            transport,
            new FakeTokenCredential());

        response.Ok.Should().BeTrue();
        response.Status.Should().Be(200);
        response.Body!["value"]!.AsArray().Should().HaveCount(1);
        response.Headers!["request-id"].Should().Be("rid");

        // ParametersNameDecodingHandler turns the escaped %24select back into $select on the wire.
        transport.Requests.Single().Uri.Query.Should().Contain("$select=id");
    }

    [Fact]
    public async Task Attaches_the_bearer_token_to_graph_requests()
    {
        var transport = new ScriptedTransport(Response(HttpStatusCode.OK, "{}"));
        var credential = new FakeTokenCredential();

        await RequestAsync(
            new RequestEnvelope { Method = "GET", Path = "/me" }, transport, credential);

        transport.Requests.Single().Authorization.Should().Be($"Bearer {FakeTokenCredential.Token}");
        credential.TokenRequests.Should().Be(1);
    }

    [Fact]
    public async Task Withholds_the_bearer_token_from_an_off_host_absolute_url()
    {
        // A pre-authenticated download URL needs no token, and must not be handed one.
        var transport = new ScriptedTransport(Response(HttpStatusCode.OK, "{}"));
        var credential = new FakeTokenCredential();

        await RequestAsync(
            new RequestEnvelope
            {
                Method = "GET",
                Path = "https://contoso.sharepoint.com/_layouts/download.aspx?id=1",
            },
            transport,
            credential);

        transport.Requests.Single().Authorization.Should().BeNull();
        credential.TokenRequests.Should().Be(0);
    }

    [Fact]
    public async Task Retries_a_429_and_honours_Retry_After()
    {
        var transport = new ScriptedTransport(
            Response((HttpStatusCode)429, """{ "error": { "code": "activityLimitReached" } }""",
                ("Retry-After", "1")),
            Response(HttpStatusCode.OK, """{ "value": [] }"""));

        var elapsed = Stopwatch.StartNew();
        var response = await RequestAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" },
            transport,
            new FakeTokenCredential());
        elapsed.Stop();

        response.Ok.Should().BeTrue();
        transport.Requests.Should().HaveCount(2);
        elapsed.Elapsed.Should().BeGreaterThanOrEqualTo(TimeSpan.FromSeconds(1));
    }

    [Fact]
    public async Task Surfaces_retryAfterSeconds_when_the_retry_budget_is_exhausted()
    {
        var transport = new ScriptedTransport(
            Response((HttpStatusCode)429, ThrottledBody, ("Retry-After", "1")),
            Response((HttpStatusCode)429, ThrottledBody, ("Retry-After", "7")));

        // Kiota's RetryHandler throws once its budget is spent rather than returning the last
        // response. Every export funnels that through ErrorEnvelope.From (§6.2), so this asserts
        // what Python actually receives. Caught by hand because the throw is an AggregateException.
        ErrorEnvelope? envelope = null;
        try
        {
            await RequestAsync(
                new RequestEnvelope { Method = "GET", Path = "/users" },
                transport,
                new FakeTokenCredential(),
                new RetrySettings { MaxRetries = 1 });
        }
        catch (Exception exception)
        {
            envelope = ErrorEnvelope.From(exception);
        }

        envelope.Should().NotBeNull("an exhausted retry budget must surface, not pass silently");

        transport.Requests.Should().HaveCount(2, "one attempt plus one retry");
        envelope.Ok.Should().BeFalse();
        envelope.Status.Should().Be(429);
        envelope.Error.Code.Should().Be("activityLimitReached");
        envelope.Error.RetryAfterSeconds.Should().Be(7, "the caller must be able to back off itself");
    }

    [Fact]
    public async Task Re_acquires_the_token_once_on_a_claims_challenge()
    {
        // Base64 of {"access_token":{"nbf":{"essential":true,"value":"1603742800"}}} — the
        // Continuous Access Evaluation challenge Entra returns when a token must be re-minted.
        const string Claims =
            "eyJhY2Nlc3NfdG9rZW4iOnsibmJmIjp7ImVzc2VudGlhbCI6dHJ1ZSwidmFsdWUiOiIxNjAzNzQyODAwIn19fQ==";

        var transport = new ScriptedTransport(
            Response(HttpStatusCode.Unauthorized, """{ "error": { "code": "InvalidAuthenticationToken" } }""",
                ("WWW-Authenticate",
                 $"Bearer realm=\"\", authorization_uri=\"https://login.microsoftonline.com/common/oauth2/authorize\", error=\"insufficient_claims\", claims=\"{Claims}\"")),
            Response(HttpStatusCode.OK, """{ "value": [] }"""));

        var credential = new FakeTokenCredential();
        var response = await RequestAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport, credential);

        response.Ok.Should().BeTrue();
        transport.Requests.Should().HaveCount(2, "the original request plus exactly one retry");
        credential.TokenRequests.Should().Be(2, "exactly one re-acquisition");
    }

    [Fact]
    public async Task Reports_a_bare_401_rather_than_retrying_it()
    {
        // Kiota re-authenticates only on a claims challenge. A plain 401 is a real failure and
        // must reach the caller as one, not disappear into a retry loop.
        var transport = new ScriptedTransport(
            Response(HttpStatusCode.Unauthorized,
                """{ "error": { "code": "InvalidAuthenticationToken", "message": "Access token is empty." } }"""));

        var response = await RequestAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" },
            transport,
            new FakeTokenCredential());

        transport.Requests.Should().HaveCount(1);
        response.Ok.Should().BeFalse();
        response.Status.Should().Be(401);
        response.Error!.Code.Should().Be("InvalidAuthenticationToken");
    }

    [Fact]
    public async Task No_credential_material_reaches_the_envelope_on_any_path()
    {
        var transport = new ScriptedTransport(
            Response(HttpStatusCode.Forbidden,
                """{ "error": { "code": "accessDenied", "message": "Insufficient privileges." } }""",
                ("WWW-Authenticate", "Bearer realm=\"graph\""),
                ("Authorization", $"Bearer {FakeTokenCredential.Token}")));

        var response = await RequestAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" },
            transport,
            new FakeTokenCredential());

        var json = JsonSerializer.Serialize(response, GraphJsonContext.Default.ResponseEnvelope);

        json.Should().NotContain(FakeTokenCredential.Token)
            .And.NotContain("Authorization")
            .And.NotContain("WWW-Authenticate");
    }

    private const string ThrottledBody = """
        { "error": { "code": "activityLimitReached", "message": "Too many requests." } }
        """;

    private static Dictionary<string, JsonElement> Query(string json)
    {
        using var document = JsonDocument.Parse(json);
        return document.RootElement.EnumerateObject()
            .ToDictionary(property => property.Name, property => property.Value.Clone());
    }
}
