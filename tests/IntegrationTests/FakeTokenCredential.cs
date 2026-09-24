using Azure.Core;

namespace IntegrationTests;

/// <summary>
/// Stands in for Azure.Identity so the pipeline can be exercised without a tenant. It deliberately
/// does not cache, which makes every token acquisition the pipeline performs countable.
/// </summary>
internal sealed class FakeTokenCredential : TokenCredential
{
    public const string Token = "fake-access-token";

    private int _tokenRequests;

    public int TokenRequests => Volatile.Read(ref _tokenRequests);

    public override AccessToken GetToken(TokenRequestContext requestContext, CancellationToken cancellationToken)
    {
        Interlocked.Increment(ref _tokenRequests);
        return new AccessToken(Token, DateTimeOffset.UtcNow.AddMinutes(30));
    }

    public override ValueTask<AccessToken> GetTokenAsync(
        TokenRequestContext requestContext, CancellationToken cancellationToken) =>
        new(GetToken(requestContext, cancellationToken));
}
