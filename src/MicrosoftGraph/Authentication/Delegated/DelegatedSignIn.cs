using MicrosoftGraph.Models;

namespace MicrosoftGraph.Authentication.Delegated;

/// <summary>Sign-in concerns shared by both delegated flows.</summary>
internal static class DelegatedSignIn
{
    /// <summary>The window a user has to finish signing in (§6.3), matching a device code's life.</summary>
    public static TimeSpan Window { get; } = TimeSpan.FromMinutes(15);

    /// <summary>
    /// Turns a failed sign-in into one of the codes §9.2 defines, so "the user declined" reads the
    /// same whichever way they were asked.
    /// </summary>
    public static GraphCoreException Translate(Exception exception) =>
        new(EntraFailure.CodeFor(exception), EntraFailure.Flatten(exception));
}
