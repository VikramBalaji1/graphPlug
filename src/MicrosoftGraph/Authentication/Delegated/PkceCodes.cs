using System.Buffers.Text;
using System.Security.Cryptography;
using System.Text;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>
/// An RFC 7636 verifier/challenge pair. <b>The verifier never crosses the ABI</b> (§7.5): it is
/// credential material for the duration of the exchange, and keeping it inside the core means
/// Python cannot leak it by logging a return value.
/// </summary>
internal sealed record PkceCodes(string Verifier, string Challenge)
{
    /// <summary>32 random bytes encode to 43 base64url characters — RFC 7636's minimum length.</summary>
    private const int EntropyBytes = 32;

    public static PkceCodes Create()
    {
        // base64url over random bytes is inherently within the unreserved set RFC 7636 allows.
        var verifier = Base64Url.EncodeToString(RandomNumberGenerator.GetBytes(EntropyBytes));
        var challenge = Base64Url.EncodeToString(SHA256.HashData(Encoding.ASCII.GetBytes(verifier)));

        return new PkceCodes(verifier, challenge);
    }

    /// <summary>
    /// The anti-forgery value. This one <i>does</i> cross the ABI, because Python must compare it
    /// against what the redirect delivers — that is exactly what <c>state</c> is for.
    /// </summary>
    public static string CreateState() =>
        Base64Url.EncodeToString(RandomNumberGenerator.GetBytes(EntropyBytes));
}
