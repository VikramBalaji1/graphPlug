using System.Text.Json.Serialization;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Models;

/// <summary>
/// Source-generated serialisation for every type that crosses the ABI (§10). Reflection-based
/// serialisation is disabled at build time, so anything missing from this list fails loudly.
/// Arbitrary Graph bodies stay as <c>JsonNode</c> and are never deserialised into a type.
/// </summary>
[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull)]
[JsonSerializable(typeof(CredentialsEnvelope))]
[JsonSerializable(typeof(RequestEnvelope))]
[JsonSerializable(typeof(ResponseEnvelope))]
[JsonSerializable(typeof(ErrorEnvelope))]
[JsonSerializable(typeof(SessionEnvelope))]
[JsonSerializable(typeof(BeginResultEnvelope))]
[JsonSerializable(typeof(AuthCompletionEnvelope))]
internal partial class GraphJsonContext : JsonSerializerContext;
