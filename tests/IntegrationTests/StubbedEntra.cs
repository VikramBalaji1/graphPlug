using System.Buffers.Text;
using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace IntegrationTests;

/// <summary>
/// A stand-in for the Entra endpoints a delegated sign-in touches, so the two-phase flow can be
/// driven end to end without a tenant. Reached through Azure.Identity's own <c>Transport</c> seam.
/// </summary>
internal sealed class StubbedEntra : HttpMessageHandler
{
    public const string TenantId = "11111111-1111-1111-1111-111111111111";
    public const string ClientId = "22222222-2222-2222-2222-222222222222";
    public const string UserCode = "FJKLMNPQ";
    public const string AccessToken = "stub-delegated-access-token";
    public const string ObjectId = "33333333-3333-3333-3333-333333333333";

    private const string Authority = $"https://login.microsoftonline.com/{TenantId}";

    private int _tokenAttempts;

    /// <summary>How many times the token endpoint answers "pending" before issuing the token.</summary>
    public int PendingPolls { get; init; }

    /// <summary>When set, the token endpoint fails with this OAuth error instead of issuing a token.</summary>
    public string? FailWith { get; init; }

    public List<string> Visited { get; } = [];

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var path = request.RequestUri!.AbsolutePath;
        Visited.Add(path);

        var response = path switch
        {
            var p when p.EndsWith("/v2.0/.well-known/openid-configuration") => Json(OpenIdConfiguration()),
            var p when p.Contains("/discovery/instance") => Json(InstanceDiscovery()),
            var p when p.EndsWith("/oauth2/v2.0/devicecode") => Json(DeviceCode()),
            var p when p.EndsWith("/oauth2/v2.0/token") => TokenResponse(),
            _ => new HttpResponseMessage(HttpStatusCode.NotFound),
        };

        response.RequestMessage = request;
        return Task.FromResult(response);
    }

    private HttpResponseMessage TokenResponse()
    {
        if (FailWith is not null)
        {
            return Json(
                new JsonObject
                {
                    ["error"] = FailWith,
                    ["error_description"] = $"{FailWith}: stubbed failure",
                },
                HttpStatusCode.BadRequest);
        }

        return Interlocked.Increment(ref _tokenAttempts) <= PendingPolls
            ? Json(
                new JsonObject
                {
                    ["error"] = "authorization_pending",
                    ["error_description"] = "AADSTS70016: pending end-user authorization",
                },
                HttpStatusCode.BadRequest)
            : Json(Token());
    }

    private static JsonObject OpenIdConfiguration() => new()
    {
        ["token_endpoint"] = $"{Authority}/oauth2/v2.0/token",
        ["authorization_endpoint"] = $"{Authority}/oauth2/v2.0/authorize",
        ["device_authorization_endpoint"] = $"{Authority}/oauth2/v2.0/devicecode",
        ["issuer"] = $"{Authority}/v2.0",
        ["tenant_region_scope"] = "NA",
        ["cloud_instance_name"] = "microsoftonline.com",
        ["cloud_graph_host_name"] = "graph.windows.net",
        ["msgraph_host"] = "graph.microsoft.com",
    };

    private static JsonObject InstanceDiscovery() => new()
    {
        ["tenant_discovery_endpoint"] = $"{Authority}/v2.0/.well-known/openid-configuration",
        ["metadata"] = new JsonArray(),
    };

    private static JsonObject DeviceCode() => new()
    {
        ["device_code"] = "stub-device-code",
        ["user_code"] = UserCode,
        ["verification_uri"] = "https://microsoft.com/devicelogin",
        ["expires_in"] = 900,
        ["interval"] = 1,
        ["message"] = $"To sign in, open https://microsoft.com/devicelogin and enter {UserCode}.",
    };

    private static JsonObject Token() => new()
    {
        ["token_type"] = "Bearer",
        ["scope"] = "User.Read",
        ["expires_in"] = 3600,
        ["ext_expires_in"] = 3600,
        ["access_token"] = AccessToken,
        ["refresh_token"] = "stub-refresh-token",
        ["id_token"] = IdToken(),
        ["client_info"] = Base64Url.EncodeToString(Encoding.UTF8.GetBytes(
            new JsonObject { ["uid"] = ObjectId, ["utid"] = TenantId }.ToJsonString())),
    };

    /// <summary>
    /// An unsigned JWT. MSAL parses the claims to build its account record and trusts TLS for
    /// authenticity, so it never checks the signature on a token endpoint response.
    /// </summary>
    private static string IdToken()
    {
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        var header = new JsonObject { ["typ"] = "JWT", ["alg"] = "none" };
        var payload = new JsonObject
        {
            ["aud"] = ClientId,
            ["iss"] = $"{Authority}/v2.0",
            ["iat"] = now,
            ["nbf"] = now,
            ["exp"] = now + 3600,
            ["sub"] = ObjectId,
            ["oid"] = ObjectId,
            ["tid"] = TenantId,
            ["preferred_username"] = "alice@contoso.com",
            ["ver"] = "2.0",
        };

        return $"{Segment(header)}.{Segment(payload)}.";
    }

    private static string Segment(JsonObject value) =>
        Base64Url.EncodeToString(Encoding.UTF8.GetBytes(value.ToJsonString()));

    private static HttpResponseMessage Json(JsonObject body, HttpStatusCode status = HttpStatusCode.OK) =>
        new(status)
        {
            Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
        };
}
