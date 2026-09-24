using System.Net;
using Azure.Identity;
using Microsoft.Kiota.Abstractions;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class ErrorMappingTests
{
    private const string ThrottledBody = """
        {
          "error": {
            "code": "activityLimitReached",
            "message": "Too many requests. Please retry after some time.",
            "innerError": {
              "code": "quotaLimitReached",
              "request-id": "a1b2c3d4",
              "date": "2026-09-22T10:14:32"
            }
          }
        }
        """;

    [Fact]
    public async Task Maps_a_graph_error_body_onto_the_error_envelope()
    {
        var transport = StubHttpMessageHandler.Returning(
            (HttpStatusCode)429,
            ThrottledBody,
            ("Retry-After", "12"),
            ("request-id", "a1b2c3d4"),
            ("client-request-id", "e5f6a7b8"));

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport);

        response.Ok.Should().BeFalse();
        response.Status.Should().Be(429);
        response.Error.Should().NotBeNull();
        response.Error!.Code.Should().Be("activityLimitReached");
        response.Error.Message.Should().Be("Too many requests. Please retry after some time.");
        response.Error.RequestId.Should().Be("a1b2c3d4");
        response.Error.ClientRequestId.Should().Be("e5f6a7b8");
        response.Error.RetryAfterSeconds.Should().Be(12);
    }

    [Fact]
    public async Task Preserves_the_graph_inner_error_verbatim()
    {
        var transport = StubHttpMessageHandler.Returning((HttpStatusCode)429, ThrottledBody);

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport);

        response.Error!.InnerError.Should().NotBeNull();
        response.Error.InnerError!["code"]!.GetValue<string>().Should().Be("quotaLimitReached");
        response.Error.InnerError["request-id"]!.GetValue<string>().Should().Be("a1b2c3d4");
    }

    [Fact]
    public async Task Parses_a_Retry_After_given_as_an_http_date()
    {
        var when = DateTimeOffset.UtcNow.AddSeconds(30).ToString("R");
        var transport = StubHttpMessageHandler.Returning(
            HttpStatusCode.ServiceUnavailable, "{}", ("Retry-After", when));

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport);

        response.Error!.RetryAfterSeconds.Should().BeInRange(25, 31);
    }

    [Fact]
    public async Task Falls_back_to_the_status_when_the_body_is_not_graph_json()
    {
        var transport = StubHttpMessageHandler.Returning(
            HttpStatusCode.BadGateway, "<html><body>Bad Gateway</body></html>");

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users" }, transport);

        response.Ok.Should().BeFalse();
        response.Status.Should().Be(502);
        response.Error!.Code.Should().Be(nameof(HttpStatusCode.BadGateway));
        response.Error.Message.Should().Contain("Bad Gateway");
    }

    [Fact]
    public async Task Omits_absent_error_fields_rather_than_nulling_them()
    {
        var transport = StubHttpMessageHandler.Returning(
            HttpStatusCode.NotFound,
            """{ "error": { "code": "itemNotFound", "message": "not found" } }""");

        var response = await OperationRunner.RunAsync(
            new RequestEnvelope { Method = "GET", Path = "/users/nope" }, transport);

        var json = System.Text.Json.JsonSerializer.Serialize(
            response, GraphJsonContext.Default.ResponseEnvelope);

        json.Should().NotContain("retryAfterSeconds").And.NotContain("innerError");
    }

    [Theory]
    [InlineData(typeof(HttpRequestException), "transportError")]
    [InlineData(typeof(TaskCanceledException), "timeout")]
    [InlineData(typeof(System.Text.Json.JsonException), "invalidRequest")]
    [InlineData(typeof(InvalidOperationException), "internalError")]
    public void Maps_non_http_failures_onto_core_error_codes(Type exceptionType, string expected)
    {
        var exception = (Exception)Activator.CreateInstance(exceptionType)!;

        ErrorEnvelope.From(exception).Error.Code.Should().Be(expected);
    }

    [Fact]
    public void Maps_a_core_exception_onto_its_own_code()
    {
        ErrorEnvelope.From(new GraphCoreException("invalidHandle", "gone"))
            .Error.Code.Should().Be("invalidHandle");
    }

    [Fact]
    public void Maps_an_unconsented_scope_onto_consentRequired()
    {
        var failure = new AuthenticationFailedException(
            "AADSTS65001: The user or administrator has not consented.");

        ErrorEnvelope.From(failure).Error.Code.Should().Be("consentRequired");
    }

    [Fact]
    public void Maps_other_azure_identity_failures_onto_authenticationFailed()
    {
        ErrorEnvelope.From(new AuthenticationFailedException("bad secret"))
            .Error.Code.Should().Be("authenticationFailed");
    }

    [Fact]
    public void Recovers_status_and_retry_information_from_an_exhausted_retry_budget()
    {
        // Kiota's RetryHandler throws instead of returning the last response, so the envelope has
        // to be rebuilt from the exception or a throttled call degrades to `internalError`.
        var api = new ApiException(
            "HTTP request failed with status code: TooManyRequests." + ThrottledBody)
        {
            ResponseStatusCode = 429,
            ResponseHeaders = new Dictionary<string, IEnumerable<string>>
            {
                ["Retry-After"] = ["7"],
                ["request-id"] = ["a1b2c3d4"],
                ["WWW-Authenticate"] = ["Bearer realm=\"graph\""],
            },
        };

        var envelope = ErrorEnvelope.From(new AggregateException("Too many retries performed.", api));

        envelope.Ok.Should().BeFalse();
        envelope.Status.Should().Be(429);
        envelope.Error.Code.Should().Be("activityLimitReached");
        envelope.Error.RetryAfterSeconds.Should().Be(7);
        envelope.Error.RequestId.Should().Be("a1b2c3d4");
        envelope.Error.InnerError!["code"]!.GetValue<string>().Should().Be("quotaLimitReached");
    }

    [Fact]
    public void Drops_non_allowlisted_headers_recovered_from_an_api_exception()
    {
        var api = new ApiException("HTTP request failed with status code: Forbidden.")
        {
            ResponseStatusCode = 403,
            ResponseHeaders = new Dictionary<string, IEnumerable<string>>
            {
                ["Authorization"] = ["Bearer super-secret"],
            },
        };

        var json = System.Text.Json.JsonSerializer.Serialize(
            ErrorEnvelope.From(api), GraphJsonContext.Default.ErrorEnvelope);

        json.Should().NotContain("super-secret").And.NotContain("Authorization");
    }

    [Fact]
    public void Falls_back_to_requestFailed_when_an_api_exception_carries_no_graph_body()
    {
        var api = new ApiException("HTTP request failed with status code: GatewayTimeout.")
        {
            ResponseStatusCode = 504,
        };

        var envelope = ErrorEnvelope.From(api);

        envelope.Status.Should().Be(504);
        envelope.Error.Code.Should().Be("requestFailed");
    }
}
