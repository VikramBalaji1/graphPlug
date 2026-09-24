namespace MicrosoftGraph.Models;

/// <summary>
/// The core's own version. The shared library and the Python package that loads it ship as one
/// unit, and a mismatched pair must fail loudly rather than subtly (§6.6).
/// </summary>
internal static class CoreVersion
{
    public const string Value = "0.1.0";
}

/// <summary>
/// A failure the core itself detects, carrying one of the codes in ARCHITECTURE.md §9.2.
/// These have no HTTP response behind them and surface with <c>status: 0</c>.
/// </summary>
internal sealed class GraphCoreException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}
