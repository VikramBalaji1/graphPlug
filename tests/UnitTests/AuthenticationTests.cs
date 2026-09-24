using Azure.Identity;
using MicrosoftGraph.Authentication;
using MicrosoftGraph.Authentication.AppOnly;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class AuthenticationTests
{
    private static CredentialsEnvelope ClientSecret() => new()
    {
        Type = "clientSecret",
        TenantId = "11111111-1111-1111-1111-111111111111",
        ClientId = "22222222-2222-2222-2222-222222222222",
        ClientSecret = "a-secret",
    };

    [Fact]
    public void Factory_maps_clientSecret_onto_the_app_only_strategy()
    {
        AuthenticationStrategyFactory.Create(ClientSecret())
            .Should().BeOfType<ClientSecretStrategy>();
    }

    [Fact]
    public void Factory_rejects_an_unknown_credential_type()
    {
        var credentials = ClientSecret() with { Type = "smokeSignals" };

        FluentActions.Invoking(() => AuthenticationStrategyFactory.Create(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("unsupportedCredentialType");
    }

    [Fact]
    public void Factory_rejects_a_missing_credential_type()
    {
        var credentials = ClientSecret() with { Type = null };

        FluentActions.Invoking(() => AuthenticationStrategyFactory.Create(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Theory]
    [InlineData("tenantId")]
    [InlineData("clientId")]
    [InlineData("clientSecret")]
    public void Client_secret_names_the_missing_required_field(string field)
    {
        var credentials = field switch
        {
            "tenantId" => ClientSecret() with { TenantId = null },
            "clientId" => ClientSecret() with { ClientId = null },
            _ => ClientSecret() with { ClientSecret = "  " },
        };

        FluentActions.Invoking(() => new ClientSecretStrategy(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Should().Match<GraphCoreException>(
                e => e.Code == "invalidRequest" && e.Message.Contains(field));
    }

    [Fact]
    public void App_only_defaults_its_scope_to_dot_default()
    {
        new ClientSecretStrategy(ClientSecret())
            .Scopes.Should().Equal(ClientSecretStrategy.DefaultScope);
    }

    [Fact]
    public void App_only_honours_explicit_scopes_when_supplied()
    {
        var credentials = ClientSecret() with { Scopes = ["https://graph.microsoft.us/.default"] };

        new ClientSecretStrategy(credentials)
            .Scopes.Should().Equal("https://graph.microsoft.us/.default");
    }

    [Fact]
    public async Task Client_secret_produces_an_azure_identity_credential()
    {
        var credential = await new ClientSecretStrategy(ClientSecret())
            .CreateCredentialAsync(CancellationToken.None);

        credential.Should().BeOfType<ClientSecretCredential>();
    }

    [Fact]
    public void Rejects_a_non_https_authority_host()
    {
        var credentials = ClientSecret() with { AuthorityHost = "http://login.example.com" };

        FluentActions.Invoking(() => new ClientSecretStrategy(credentials))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public async Task An_interactive_strategy_reached_through_the_single_shot_path_raises_interactionRequired()
    {
        var act = () => new InteractiveOnlyStrategy().CreateCredentialAsync(CancellationToken.None);

        (await act.Should().ThrowAsync<GraphCoreException>())
            .Which.Code.Should().Be("interactionRequired");
    }

    /// <summary>Stands in for the delegated strategies until they land (§15 milestone 3).</summary>
    private sealed class InteractiveOnlyStrategy : AuthenticationStrategy
    {
        public override IReadOnlyList<string> Scopes => ["User.Read"];
    }
}
