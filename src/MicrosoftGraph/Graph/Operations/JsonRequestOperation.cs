using System.Text;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Graph.Operations;

/// <summary>An ordinary JSON request to any <c>v1.0</c> or <c>beta</c> endpoint.</summary>
internal sealed class JsonRequestOperation(RequestEnvelope request) : GraphOperation(request.Headers)
{
    private const string NextLinkAnnotation = "@odata.nextLink";

    protected override HttpRequestMessage BuildRequest(GraphUrlBuilder urls)
    {
        var message = new HttpRequestMessage(
            ResolveMethod(request.Method),
            urls.Build(request.Path, request.Version, request.Query));

        if (request.Body is not null)
        {
            // ToJsonString round-trips the caller's body untouched and needs no serialiser context.
            message.Content = new StringContent(
                request.Body.ToJsonString(), Encoding.UTF8, "application/json");
        }

        return message;
    }

    protected override async Task<ResponseEnvelope> MapSuccessAsync(
        HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var body = await ReadJsonBodyAsync(response, cancellationToken).ConfigureAwait(false);

        return new ResponseEnvelope
        {
            Ok = true,
            Status = (int)response.StatusCode,
            Headers = ResponseHeaderFilter.Apply(response),
            Body = body,

            // Promoted to the top level so Python never needs to know the OData annotation name.
            // It is left in place in `body` as well, so the Graph response stays verbatim.
            NextLink = body?[NextLinkAnnotation]?.GetValue<string>(),
        };
    }

    private static HttpMethod ResolveMethod(string? method) =>
        string.IsNullOrWhiteSpace(method)
            ? throw new GraphCoreException("invalidRequest", "'method' is required")
            : HttpMethod.Parse(method);
}
