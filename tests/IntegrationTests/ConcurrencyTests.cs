using System.Collections.Concurrent;
using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using MicrosoftGraph.Authentication.AppOnly;
using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models.Envelopes;

namespace IntegrationTests;

/// <summary>
/// A handle is safe to share across threads (§8.9): <see cref="HttpClient"/> is thread-safe,
/// Azure.Identity serialises token refresh, and the registries are concurrent dictionaries. This
/// is the test that says so rather than the comment.
/// </summary>
public class ConcurrencyTests
{
    private const int Threads = 8;
    private const int PerThread = 25;

    private static readonly string[] Scopes = [ClientSecretStrategy.DefaultScope];

    /// <summary>
    /// Answers every request with a fresh response, recording what it saw. Unlike
    /// <see cref="ScriptedTransport"/> this holds no queue, so it is safe to hit in parallel.
    /// </summary>
    private sealed class ConcurrentTransport : HttpMessageHandler
    {
        private int _served;

        public ConcurrentBag<string> Paths { get; } = [];

        public ConcurrentBag<string?> Authorizations { get; } = [];

        public int Served => Volatile.Read(ref _served);

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Paths.Add(request.RequestUri!.AbsolutePath);
            Authorizations.Add(request.Headers.Authorization?.ToString());
            var ordinal = Interlocked.Increment(ref _served);

            var body = new JsonObject { ["value"] = new JsonArray(), ["n"] = ordinal };
            var response = new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
                RequestMessage = request,
            };
            response.Headers.TryAddWithoutValidation("request-id", $"rid-{ordinal}");

            return Task.FromResult(response);
        }
    }

    [Fact]
    public async Task One_session_serves_many_threads_at_once()
    {
        var transport = new ConcurrentTransport();
        var credential = new FakeTokenCredential();

        await using var session = GraphSession.Create(credential, Scopes, finalHandler: transport);

        var work = Enumerable.Range(0, Threads).Select(thread => Task.Run(async () =>
        {
            var seen = new List<ResponseEnvelope>();
            for (var i = 0; i < PerThread; i++)
            {
                seen.Add(await session.Executor.ExecuteAsync(
                    new JsonRequestOperation(new RequestEnvelope
                    {
                        Method = "GET",
                        Path = $"/users/thread-{thread}-{i}",
                    }),
                    CancellationToken.None));
            }

            return seen;
        }));

        var results = (await Task.WhenAll(work)).SelectMany(r => r).ToList();

        results.Should().HaveCount(Threads * PerThread);
        results.Should().OnlyContain(r => r.Ok && r.Status == 200);
        transport.Served.Should().Be(Threads * PerThread);

        // Every request reached the wire exactly once, under its own path.
        transport.Paths.Distinct().Should().HaveCount(Threads * PerThread);
    }

    [Fact]
    public async Task Every_concurrent_request_carries_the_bearer_token()
    {
        var transport = new ConcurrentTransport();

        await using var session = GraphSession.Create(
            new FakeTokenCredential(), Scopes, finalHandler: transport);

        await Task.WhenAll(Enumerable.Range(0, Threads * 4).Select(i => Task.Run(() =>
            session.Executor.ExecuteAsync(
                new JsonRequestOperation(new RequestEnvelope { Method = "GET", Path = $"/u/{i}" }),
                CancellationToken.None))));

        transport.Authorizations.Should().OnlyContain(
            a => a == $"Bearer {FakeTokenCredential.Token}",
            "a race in the auth handler would show up as a missing or stale header");
    }

    [Fact]
    public async Task Responses_are_not_crossed_between_threads()
    {
        // Each request asks for a distinct path; each response echoes a distinct ordinal. If the
        // pipeline ever handed one thread another's response, the counts would not line up.
        var transport = new ConcurrentTransport();

        await using var session = GraphSession.Create(
            new FakeTokenCredential(), Scopes, finalHandler: transport);

        var ordinals = await Task.WhenAll(Enumerable.Range(0, Threads * PerThread).Select(i =>
            Task.Run(async () =>
            {
                var response = await session.Executor.ExecuteAsync(
                    new JsonRequestOperation(new RequestEnvelope
                    {
                        Method = "GET",
                        Path = $"/users/{i}",
                    }),
                    CancellationToken.None);

                return response.Body!["n"]!.GetValue<int>();
            })));

        ordinals.Should().OnlyHaveUniqueItems("every caller must get its own response");
        ordinals.Should().HaveCount(Threads * PerThread);
    }

    [Fact]
    public async Task Paging_from_several_threads_keeps_each_caller_on_its_own_cursor()
    {
        // The nearest thing to what a Python script does: several threads walking pages at once.
        var transport = new ConcurrentTransport();

        await using var session = GraphSession.Create(
            new FakeTokenCredential(), Scopes, finalHandler: transport);

        async Task<int> WalkAsync(int thread)
        {
            var pages = 0;
            for (var page = 0; page < 5; page++)
            {
                var response = await session.Executor.ExecuteAsync(
                    new JsonRequestOperation(new RequestEnvelope
                    {
                        Method = "GET",
                        Path = $"/users/t{thread}/p{page}",
                    }),
                    CancellationToken.None);

                response.Ok.Should().BeTrue();
                pages++;
            }

            return pages;
        }

        var walked = await Task.WhenAll(Enumerable.Range(0, Threads).Select(WalkAsync));

        walked.Should().AllBeEquivalentTo(5);
        transport.Served.Should().Be(Threads * 5);
    }

    [Fact]
    public async Task Closing_a_session_while_requests_are_in_flight_does_not_corrupt_anything()
    {
        var transport = new ConcurrentTransport();
        var session = GraphSession.Create(new FakeTokenCredential(), Scopes, finalHandler: transport);

        var inFlight = Enumerable.Range(0, 40).Select(i => Task.Run(async () =>
        {
            try
            {
                await session.Executor.ExecuteAsync(
                    new JsonRequestOperation(new RequestEnvelope { Method = "GET", Path = $"/u/{i}" }),
                    CancellationToken.None);
                return true;
            }
            catch (Exception)
            {
                // A request racing disposal may fail. What must not happen is a hang or a crash.
                return false;
            }
        })).ToArray();

        await Task.WhenAll(inFlight).WaitAsync(TimeSpan.FromSeconds(30));
        await session.DisposeAsync();

        inFlight.Should().OnlyContain(t => t.IsCompletedSuccessfully);
    }
}
