namespace MicrosoftGraph.Models;

/// <summary>
/// The core's own version. The shared library and the Python package that loads it ship as one
/// unit, and a mismatched pair must fail loudly rather than subtly (§6.6).
/// </summary>
internal static class CoreVersion
{
    public const string Value = "0.1.0";
}
