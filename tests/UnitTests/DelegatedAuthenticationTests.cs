using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Web;
using MicrosoftGraph.Authentication;
using MicrosoftGraph.Authentication.Delegated;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class DelegatedAuthenticationTests
{
    private const string Tenant = "11111111-1111-1111-1111-111111111111";
    private const string Client = "22222222-2222-2222-2222-222222222222";

    private static CredentialsEnvelope Delegated(string type) => new()
    {
        Type = type,
        TenantId = Tenant,
        ClientId = Client,
        Scopes = ["User.Read", "Mail.Send"],
        RedirectUri = "http://localhost:8400",
    };

    // ── factory and access model ─────────────────────────────────────────────

    [Theory]
    [InlineData("deviceCode", typeof(DeviceCodeStrategy))]
    [InlineData("authorizationCode", typeof(AuthorizationCodeStrategy))]
    public void Factory_maps_each_delegated_type_onto_its_strategy(string type, Type expected)
    {
        AuthenticationStrategyFactory.Create(Delegated(type)).Should().BeOfType(expected);
    }

    [Theory]
    [InlineData("deviceCode")]
    [InlineData("authorizationCode")]
    public void Delegated_strategies_report_the_delegated_model_and_need_a_human(string type)
    {
        var strategy = AuthenticationStrategyFactory.Create(Delegated(type));

        strategy.AccessModel.Should().Be(AccessModel.Delegated);
        strategy.RequiresInteraction.Should().BeTrue();
    }

    // ── scopes are never defaulted (§7.3) ────────────────────────────────────

    [Theory]
    [InlineData("deviceCode")]
    [InlineData("authorizationCode")]
    public void A_delegated_envelope_without_scopes_is_rejected(string type)
    {
        var credentials = Delegated(type) with { Scopes = null };

        FluentActions.Invoking(() => AuthenticationStrategyFactory.Create(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Should().Match<GraphCoreException>(
                e => e.Code == "invalidRequest" && e.Message.Contains("scopes"));
    }

    [Theory]
    [InlineData("deviceCode")]
    [InlineData("authorizationCode")]
    public void An_empty_scope_list_is_rejected_too(string type)
    {
        var credentials = Delegated(type) with { Scopes = [] };

        FluentActions.Invoking(() => AuthenticationStrategyFactory.Create(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Theory]
    [InlineData("deviceCode")]
    [InlineData("authorizationCode")]
    public async Task A_two_phase_strategy_used_single_shot_raises_interactionRequired(string type)
    {
        var strategy = AuthenticationStrategyFactory.Create(Delegated(type));
        var act = () => strategy.CreateCredentialAsync(CancellationToken.None);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("interactionRequired");
    }

    [Fact]
    public void An_app_only_strategy_asked_for_a_pending_flow_raises_interactionNotSupported()
    {
        var credentials = new CredentialsEnvelope
        {
            Type = "clientSecret",
            TenantId = Tenant,
            ClientId = Client,
            ClientSecret = "s",
        };

        FluentActions
            .Invoking(() => AuthenticationStrategyFactory.Create(credentials).CreatePendingAuthentication())
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("interactionNotSupported");
    }

    // ── redirect URI ─────────────────────────────────────────────────────────

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("not a url")]
    public void The_authorization_code_flow_requires_a_usable_redirect_uri(string? redirectUri)
    {
        var credentials = Delegated("authorizationCode") with { RedirectUri = redirectUri };

        FluentActions.Invoking(() => new AuthorizationCodeStrategy(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public void A_redirect_uri_off_this_machine_is_refused()
    {
        // Anything but loopback would send the authorization code somewhere else entirely.
        var credentials = Delegated("authorizationCode") with { RedirectUri = "https://evil.example/cb" };

        FluentActions.Invoking(() => new AuthorizationCodeStrategy(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Theory]
    [InlineData("http://localhost:8400")]
    [InlineData("http://127.0.0.1:8400/callback")]
    public void Loopback_redirect_uris_are_accepted(string redirectUri)
    {
        var credentials = Delegated("authorizationCode") with { RedirectUri = redirectUri };

        FluentActions.Invoking(() => new AuthorizationCodeStrategy(credentials)).Should().NotThrow();
    }

    // ── PKCE (§7.5, RFC 7636) ────────────────────────────────────────────────

    [Fact]
    public void The_verifier_meets_RFC_7636_length_and_charset()
    {
        var pkce = PkceCodes.Create();

        pkce.Verifier.Length.Should().BeInRange(43, 128);
        pkce.Verifier.Should().MatchRegex("^[A-Za-z0-9._~-]+$");
    }

    [Fact]
    public void The_challenge_is_the_S256_transform_of_the_verifier()
    {
        var pkce = PkceCodes.Create();

        var expected = System.Buffers.Text.Base64Url.EncodeToString(
            SHA256.HashData(Encoding.ASCII.GetBytes(pkce.Verifier)));

        pkce.Challenge.Should().Be(expected);
    }

    [Fact]
    public void Every_verifier_and_state_is_fresh()
    {
        var verifiers = Enumerable.Range(0, 50).Select(_ => PkceCodes.Create().Verifier).ToList();
        var states = Enumerable.Range(0, 50).Select(_ => PkceCodes.CreateState()).ToList();

        verifiers.Should().OnlyHaveUniqueItems();
        states.Should().OnlyHaveUniqueItems();
    }

    [Fact]
    public async Task The_verifier_never_appears_in_any_envelope()
    {
        await using var pending = NewAuthorizationCodeFlow();
        var begun = await pending.BeginAsync(CancellationToken.None);

        var json = JsonSerializer.Serialize(begun, GraphJsonContext.Default.BeginResultEnvelope);
        var verifier = VerifierOf(pending);

        json.Should().NotContain(verifier);
        json.Should().NotContain("verifier").And.NotContain("code_verifier");

        // The challenge does travel, inside the authorize URL — that is what it is for.
        json.Should().Contain("code_challenge");
    }

    // ── the authorize URL ────────────────────────────────────────────────────

    [Fact]
    public async Task Begin_builds_an_authorize_url_with_the_S256_challenge_and_state()
    {
        await using var pending = NewAuthorizationCodeFlow();
        var begun = await pending.BeginAsync(CancellationToken.None);

        var url = new Uri(begun.AuthorizeUrl!);
        var query = HttpUtility.ParseQueryString(url.Query);

        url.GetLeftPart(UriPartial.Path).Should().Be(
            $"https://login.microsoftonline.com/{Tenant}/oauth2/v2.0/authorize");
        query["client_id"].Should().Be(Client);
        query["response_type"].Should().Be("code");
        query["redirect_uri"].Should().Be("http://localhost:8400");
        query["code_challenge_method"].Should().Be("S256");
        query["code_challenge"].Should().Be(ChallengeOf(pending));
        query["state"].Should().Be(begun.State);
        begun.State.Should().NotBeNullOrWhiteSpace();
    }

    [Fact]
    public async Task The_authorize_url_requests_offline_access_so_the_session_can_refresh()
    {
        await using var pending = NewAuthorizationCodeFlow();
        var begun = await pending.BeginAsync(CancellationToken.None);

        var scope = HttpUtility.ParseQueryString(new Uri(begun.AuthorizeUrl!).Query)["scope"];

        scope.Should().Be("User.Read Mail.Send offline_access");
    }

    [Fact]
    public async Task Begin_does_not_reach_the_network_and_sets_the_sign_in_window()
    {
        await using var pending = NewAuthorizationCodeFlow();
        var begun = await pending.BeginAsync(CancellationToken.None);

        begun.Ok.Should().BeTrue();
        begun.ExpiresInSeconds.Should().Be((int)DelegatedSignIn.Window.TotalSeconds);
        pending.HasExpired.Should().BeFalse();
        begun.UserCode.Should().BeNull("that belongs to the device code flow");
    }

    // ── state validation (§7.5) ──────────────────────────────────────────────

    [Fact]
    public async Task A_mismatched_state_raises_stateMismatch()
    {
        await using var pending = NewAuthorizationCodeFlow();
        await pending.BeginAsync(CancellationToken.None);

        var act = () => pending.CompleteAsync(
            """{ "code": "the-code", "state": "not-the-state" }""", CancellationToken.None);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("stateMismatch");
    }

    [Fact]
    public async Task A_missing_state_raises_stateMismatch_rather_than_being_skipped()
    {
        await using var pending = NewAuthorizationCodeFlow();
        await pending.BeginAsync(CancellationToken.None);

        var act = () => pending.CompleteAsync("""{ "code": "the-code" }""", CancellationToken.None);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("stateMismatch");
    }

    [Fact]
    public async Task A_matching_state_with_no_code_raises_invalidRequest()
    {
        await using var pending = NewAuthorizationCodeFlow();
        var begun = await pending.BeginAsync(CancellationToken.None);

        var act = () => pending.CompleteAsync(
            $$"""{ "state": "{{begun.State}}" }""", CancellationToken.None);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public async Task Completing_a_flow_that_was_never_begun_raises_invalidRequest()
    {
        await using var pending = NewAuthorizationCodeFlow();

        var act = () => pending.CompleteAsync("""{ "code": "c", "state": "s" }""", CancellationToken.None);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public async Task Disposing_clears_the_verifier_and_state()
    {
        var pending = NewAuthorizationCodeFlow();
        await pending.BeginAsync(CancellationToken.None);
        await pending.DisposeAsync();

        VerifierOf(pending).Should().BeNull();
    }

    // ── sign-in failure translation (§9.2) ───────────────────────────────────

    [Theory]
    [InlineData("AADSTS65001: The user or administrator has not consented", "consentRequired")]
    [InlineData("AADSTS65004: User declined to consent", "signInDeclined")]
    [InlineData("AADSTS70016: authorization_pending expired_token", "signInTimeout")]
    [InlineData("authorization_declined by the user", "signInDeclined")]
    [InlineData("access_denied", "signInDeclined")]
    [InlineData("something else entirely", "authenticationFailed")]
    public void A_failed_sign_in_maps_onto_a_defined_code(string message, string expected)
    {
        DelegatedSignIn.Translate(new InvalidOperationException(message))
            .Code.Should().Be(expected);
    }

    [Fact]
    public void The_detail_is_read_from_the_inner_exception_not_just_the_top_one()
    {
        // Azure.Identity's own message is an empty prefix; MSAL's AADSTS number is underneath it.
        var wrapped = new Azure.Identity.AuthenticationFailedException(
            "DeviceCodeCredential authentication failed: ",
            new InvalidOperationException("AADSTS65004: User declined to consent"));

        var translated = DelegatedSignIn.Translate(wrapped);

        translated.Code.Should().Be("signInDeclined");
        translated.Message.Should().Contain("AADSTS65004");
    }

    [Fact]
    public void An_error_envelope_built_from_a_wrapped_failure_keeps_the_detail_too()
    {
        var wrapped = new Azure.Identity.AuthenticationFailedException(
            "ClientSecretCredential authentication failed: ",
            new InvalidOperationException("AADSTS65001: no consent has been recorded"));

        var envelope = ErrorEnvelope.From(wrapped);

        envelope.Error.Code.Should().Be("consentRequired");
        envelope.Error.Message.Should().Contain("AADSTS65001");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static PendingAuthentication NewAuthorizationCodeFlow() =>
        new AuthorizationCodeStrategy(Delegated("authorizationCode")).CreatePendingAuthentication();

    private static string? VerifierOf(PendingAuthentication pending) => PkceOf(pending)?.Verifier;

    private static string? ChallengeOf(PendingAuthentication pending) => PkceOf(pending)?.Challenge;

    private static PkceCodes? PkceOf(PendingAuthentication pending) =>
        (PkceCodes?)pending.GetType()
            .GetField("_pkce", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)!
            .GetValue(pending);
}
