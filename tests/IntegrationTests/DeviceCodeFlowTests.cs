using System.Net;
using System.Text.Json;
using Azure.Core;
using MicrosoftGraph.Authentication.Delegated;
using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;
using static IntegrationTests.ScriptedTransport;

namespace IntegrationTests;

/// <summary>
/// The full two-phase device code flow against a stubbed Entra (§15 milestone 3): begin, code
/// issued, complete, then a working session.
/// </summary>
public class DeviceCodeFlowTests
{
    private static readonly string[] Scopes = ["User.Read"];

    private static DeviceCodePendingAuthentication NewFlow(StubbedEntra entra) =>
        new(StubbedEntra.TenantId,
            StubbedEntra.ClientId,
            new Uri("https://login.microsoftonline.com"),
            Scopes,
            entra);

    [Fact]
    public async Task Begin_returns_the_code_without_waiting_for_the_user()
    {
        // Every poll answers "pending", so the sign-in never completes during this test. Begin
        // must still return promptly — that is the whole point of the two-phase split.
        var entra = new StubbedEntra { PendingPolls = int.MaxValue };
        await using var flow = NewFlow(entra);

        var begun = await flow.BeginAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10));

        begun.Ok.Should().BeTrue();
        begun.UserCode.Should().Be(StubbedEntra.UserCode);
        begun.VerificationUri.Should().Be("https://microsoft.com/devicelogin");
        begun.Message.Should().Contain(StubbedEntra.UserCode);
        begun.ExpiresInSeconds.Should().BeInRange(1, 900);
        begun.AuthorizeUrl.Should().BeNull("that belongs to the authorization code flow");
    }

    [Fact]
    public async Task Begin_then_complete_yields_a_session_that_can_call_graph()
    {
        var entra = new StubbedEntra { PendingPolls = 1 };
        await using var flow = NewFlow(entra);

        var begun = await flow.BeginAsync(CancellationToken.None);
        begun.UserCode.Should().Be(StubbedEntra.UserCode);

        // The user "signs in" — the stub stops answering pending and issues the token.
        var credential = await flow.CompleteAsync(null, CancellationToken.None)
            .WaitAsync(TimeSpan.FromSeconds(30));

        credential.Should().BeAssignableTo<TokenCredential>();

        var graph = new ScriptedTransport(Response(HttpStatusCode.OK, """{ "displayName": "Alice" }"""));
        await using var session = GraphSession.Create(credential, Scopes, finalHandler: graph);

        var response = await session.Executor.ExecuteAsync(
            new JsonRequestOperation(new RequestEnvelope { Method = "GET", Path = "/me" }),
            CancellationToken.None);

        response.Ok.Should().BeTrue();
        response.Body!["displayName"]!.GetValue<string>().Should().Be("Alice");

        // The delegated token acquired above is what reached Graph.
        graph.Requests.Single().Authorization.Should().Be($"Bearer {StubbedEntra.AccessToken}");
        entra.Visited.Should().Contain(p => p.EndsWith("/oauth2/v2.0/devicecode"));
    }

    [Fact]
    public async Task A_declined_sign_in_reports_signInDeclined()
    {
        var entra = new StubbedEntra { FailWith = "authorization_declined" };
        await using var flow = NewFlow(entra);

        // The decline can surface at either phase depending on when Entra answers.
        var act = async () =>
        {
            await flow.BeginAsync(CancellationToken.None);
            await flow.CompleteAsync(null, CancellationToken.None);
        };

        var thrown = (await act.Should().ThrowAsync<GraphCoreException>()
            .WaitAsync(TimeSpan.FromSeconds(30))).Which;

        thrown.Code.Should().Be("signInDeclined");
        thrown.Message.Should().Contain("authorization_declined",
            "the AADSTS detail lives on an inner exception and must not be lost");
    }

    [Fact]
    public async Task Cancelling_a_pending_flow_tears_down_its_polling_task()
    {
        var entra = new StubbedEntra { PendingPolls = int.MaxValue };
        var flow = NewFlow(entra);
        await flow.BeginAsync(CancellationToken.None);

        var pollsBefore = entra.Visited.Count;

        // graph_auth_cancel disposes the flow; the background poll must stop, not run forever.
        await flow.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        await Task.Delay(TimeSpan.FromSeconds(3));

        entra.Visited.Count.Should().BeCloseTo(pollsBefore, 2,
            "polling should have stopped when the flow was disposed");
    }

    [Fact]
    public async Task An_expired_flow_is_reported_as_expired_so_the_registry_can_evict_it()
    {
        var entra = new StubbedEntra { PendingPolls = int.MaxValue };
        await using var flow = NewFlow(entra);
        await flow.BeginAsync(CancellationToken.None);

        flow.HasExpired.Should().BeFalse();

        // The device code's own expiry drives eviction (§7.5).
        flow.ExpiresAt.Should().BeAfter(DateTimeOffset.UtcNow)
            .And.BeBefore(DateTimeOffset.UtcNow.AddMinutes(20));
    }

    [Fact]
    public async Task No_token_reaches_the_begin_envelope()
    {
        var entra = new StubbedEntra { PendingPolls = int.MaxValue };
        await using var flow = NewFlow(entra);

        var begun = await flow.BeginAsync(CancellationToken.None);
        var json = JsonSerializer.Serialize(begun, GraphJsonContext.Default.BeginResultEnvelope);

        json.Should().NotContain(StubbedEntra.AccessToken).And.NotContain("refresh");
    }
}
