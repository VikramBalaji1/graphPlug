namespace MicrosoftGraph.Models;

/// <summary>
/// A failure the core itself detects, carrying one of the codes in ARCHITECTURE.md §9.2.
/// These have no HTTP response behind them and surface with <c>status: 0</c>.
/// </summary>
internal sealed class GraphCoreException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}
