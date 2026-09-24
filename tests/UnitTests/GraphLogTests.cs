using System.Net;
using System.Text.Json.Nodes;
using MicrosoftGraph.Diagnostics;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class GraphLogTests : IDisposable
{
    private readonly StringWriter _captured = new();
    private readonly GraphLogLevel _configured = GraphLog.Level;

    private IDisposable _capture;

    public GraphLogTests()
    {
        // Scoped to this test's asynchronous flow, so these run alongside everything else.
        _capture = GraphLog.Capture(GraphLogLevel.Info, _captured);
    }

    public void Dispose()
    {
        _capture.Dispose();
        _captured.Dispose();
        GC.SuppressFinalize(this);
    }

    /// <summary>Re-scopes this test at a different level.</summary>
    private void CaptureAt(GraphLogLevel level)
    {
        _capture.Dispose();
        _capture = GraphLog.Capture(level, _captured);
    }

    private string Output => _captured.ToString();

    private IEnumerable<JsonNode> Lines =>
        Output.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(line => JsonNode.Parse(line)!);

    [Fact]
    public async Task Logs_nothing_at_all_when_switched_off()
    {
        CaptureAt(GraphLogLevel.Off);

        await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" },
            RecordingTransport.Returning(HttpStatusCode.OK, "{}"));

        Output.Should().BeEmpty("a library must be silent unless asked");
    }

    [Theory]
    [InlineData(null, "Off")]
    [InlineData("", "Off")]
    [InlineData("off", "Off")]
    [InlineData("verbose", "Off")]
    [InlineData("INFO", "Info")]
    [InlineData("  Error ", "Error")]
    public void Reads_its_level_from_the_environment(string? value, string expected)
    {
        // Unrecognised values fall back to off: a typo must not silently enable logging, and the
        // absence of the variable must leave the library silent.
        GraphLog.ParseLevel(value).ToString().Should().Be(expected);
    }

    [Fact]
    public async Task Logs_a_completed_request_as_one_json_object()
    {
        await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" },
            RecordingTransport.Returning(
                HttpStatusCode.OK, """{ "value": [] }""", ("request-id", "rid-42")));

        var line = Lines.Should().ContainSingle().Which;

        line["level"]!.GetValue<string>().Should().Be("info");
        line["event"]!.GetValue<string>().Should().Be("request");
        line["method"]!.GetValue<string>().Should().Be("GET");
        line["url"]!.GetValue<string>().Should().Be("https://graph.microsoft.com/v1.0/users");
        line["status"]!.GetValue<int>().Should().Be(200);
        line["requestId"]!.GetValue<string>().Should().Be("rid-42");
        line["ms"]!.GetValue<long>().Should().BeGreaterThanOrEqualTo(0);
        line["errorCode"].Should().BeNull();
    }

    [Fact]
    public async Task Logs_a_failed_request_with_its_graph_error_code()
    {
        await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users/nope" },
            RecordingTransport.Returning(
                HttpStatusCode.NotFound,
                """{ "error": { "code": "itemNotFound", "message": "not found" } }"""));

        var line = Lines.Should().ContainSingle().Which;

        line["level"]!.GetValue<string>().Should().Be("error");
        line["status"]!.GetValue<int>().Should().Be(404);
        line["errorCode"]!.GetValue<string>().Should().Be("itemNotFound");
    }

    [Fact]
    public async Task Reports_failures_but_not_successes_at_the_error_level()
    {
        CaptureAt(GraphLogLevel.Error);

        await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" },
            RecordingTransport.Returning(HttpStatusCode.OK, "{}"));

        Output.Should().BeEmpty();

        await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" },
            RecordingTransport.Returning(HttpStatusCode.Forbidden, "{}"));

        Lines.Should().ContainSingle().Which["status"]!.GetValue<int>().Should().Be(403);
    }

    [Fact]
    public async Task Never_logs_the_query_string()
    {
        // An OData filter routinely carries user identifiers, so the URL is logged without it.
        await OperationRunner.RunAsync(
            new RequestEnvelope
            {
                Method = "GET",
                Path = "/users",
                Query = new Dictionary<string, System.Text.Json.JsonElement>
                {
                    ["$filter"] = System.Text.Json.JsonDocument
                        .Parse("\"mail eq 'alice@contoso.com'\"").RootElement.Clone(),
                },
            },
            RecordingTransport.Returning(HttpStatusCode.OK, "{}"));

        Output.Should().NotContain("alice@contoso.com").And.NotContain("filter");
        Lines.Single()["url"]!.GetValue<string>()
            .Should().Be("https://graph.microsoft.com/v1.0/users");
    }

    [Fact]
    public async Task Never_logs_a_skiptoken_from_an_echoed_nextLink()
    {
        await OperationRunner.RunAsync(
            new RequestEnvelope
            {
                Method = "GET",
                Path = "https://graph.microsoft.com/v1.0/users?$skiptoken=SECRET-CURSOR",
            },
            RecordingTransport.Returning(HttpStatusCode.OK, "{}"));

        Output.Should().NotContain("SECRET-CURSOR");
    }

    [Fact]
    public async Task Never_logs_headers_or_credential_material()
    {
        await OperationRunner.RunAsync(
            new RequestEnvelope
            {
                Method = "POST",
                Path = "/users",
                Headers = new Dictionary<string, string> { ["ConsistencyLevel"] = "eventual" },
                Body = JsonNode.Parse("""{ "passwordProfile": { "password": "hunter2" } }"""),
            },
            RecordingTransport.Returning(
                HttpStatusCode.Unauthorized,
                """{ "error": { "code": "InvalidAuthenticationToken" } }""",
                ("WWW-Authenticate", "Bearer realm=\"graph\""),
                ("Authorization", "Bearer super-secret-token")));

        Output.Should().NotContain("super-secret-token")
            .And.NotContain("Authorization")
            .And.NotContain("WWW-Authenticate")
            .And.NotContain("hunter2")
            .And.NotContain("ConsistencyLevel");
    }

    [Fact]
    public void Escapes_values_so_one_event_can_never_span_two_lines()
    {
        GraphLog.BoundaryFailure("graph_request", "internalError", "broke\non \"two\" lines");

        Output.Trim().Should().NotContain("\n");
        Lines.Single()["message"]!.GetValue<string>().Should().Be("broke\non \"two\" lines");
    }

    [Fact]
    public void Logs_session_lifecycle_with_the_handle()
    {
        GraphLog.SessionEvent("sessionCreated", 7);

        var line = Lines.Should().ContainSingle().Which;
        line["event"]!.GetValue<string>().Should().Be("sessionCreated");
        line["handle"]!.GetValue<long>().Should().Be(7);
    }

    [Fact]
    public void A_broken_writer_never_takes_the_process_down()
    {
        using var broken = GraphLog.Capture(GraphLogLevel.Info, new ThrowingWriter());

        FluentActions.Invoking(() => GraphLog.SessionEvent("sessionCreated", 1))
            .Should().NotThrow("a diagnostic must never be the thing that kills the caller");
    }

    [Fact]
    public void The_configured_level_is_untouched_by_a_capture()
    {
        // The override rides on AsyncLocal, so the process-wide setting is never mutated.
        _configured.Should().Be(GraphLog.ParseLevel(
            Environment.GetEnvironmentVariable(GraphLog.LevelVariable)));
    }

    [Fact]
    public async Task Two_concurrent_captures_do_not_see_each_other()
    {
        // The reason the whole suite can run in parallel again.
        async Task<string> Isolated(string name)
        {
            await using var writer = new StringWriter();
            using (GraphLog.Capture(GraphLogLevel.Info, writer))
            {
                await Task.Yield();
                GraphLog.SessionEvent(name, 1);
                await Task.Yield();
                return writer.ToString();
            }
        }

        var results = await Task.WhenAll(
            Enumerable.Range(0, 24).Select(i => Isolated($"flow-{i}")));

        for (var i = 0; i < results.Length; i++)
        {
            results[i].Should().Contain($"flow-{i}");
            results[i].Split(Environment.NewLine, StringSplitOptions.RemoveEmptyEntries)
                .Should().HaveCount(1, "each flow sees only its own line");
        }
    }

    private sealed class ThrowingWriter : TextWriter
    {
        public override System.Text.Encoding Encoding => System.Text.Encoding.UTF8;

        public override void WriteLine(string? value) => throw new IOException("stderr is gone");
    }
}
