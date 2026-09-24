using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using MicrosoftGraph.Authentication;
using MicrosoftGraph.Authentication.Delegated;
using MicrosoftGraph.Diagnostics;
using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace MicrosoftGraph.Interop;

/// <summary>
/// The native entrypoints (§6.1). Every function returns a pointer to a NUL-terminated UTF-8 JSON
/// envelope that the caller must release with <c>graph_free</c>.
/// </summary>
/// <remarks>
/// This file contains no logic beyond marshalling, dispatch and the mandatory catch. Every
/// behavioural decision lives in a class a unit test can construct without going near the ABI.
/// </remarks>
internal static class Exports
{
    /// <summary>Compared against the wheel's version at load time; the pair ships as one unit (§6.6).</summary>
    public const string CoreVersion = "0.1.0";

    private static readonly HandleRegistry<GraphSession> Sessions = new();

    private static readonly HandleRegistry<PendingAuthentication> PendingFlows = new();

    [UnmanagedCallersOnly(EntryPoint = "graph_client_create")]
    public static IntPtr GraphClientCreate(IntPtr credentialsJson)
    {
        try
        {
            var credentials = Parse(credentialsJson, GraphJsonContext.Default.CredentialsEnvelope);
            var strategy = AuthenticationStrategyFactory.Create(credentials);

            using var cts = new CancellationTokenSource(RequestEnvelope.DefaultTimeoutMs);
            var credential = strategy
                .CreateCredentialAsync(cts.Token)
                .GetAwaiter()
                .GetResult();

            var session = GraphSession.Create(credential, strategy.Scopes, credentials.Retry);
            var handle = Sessions.Add(session);
            GraphLog.SessionEvent("sessionCreated", handle);

            return Emit(
                new SessionEnvelope { Ok = true, Handle = handle, CoreVersion = CoreVersion },
                GraphJsonContext.Default.SessionEnvelope);
        }
        catch (Exception ex)
        {
            return EmitError("graph_client_create", ex);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "graph_auth_begin")]
    public static IntPtr GraphAuthBegin(IntPtr credentialsJson)
    {
        long flowId = 0;
        try
        {
            EvictExpiredFlows();

            var credentials = Parse(credentialsJson, GraphJsonContext.Default.CredentialsEnvelope);
            var pending = AuthenticationStrategyFactory.Create(credentials).CreatePendingAuthentication();

            flowId = PendingFlows.Add(pending);

            using var cts = new CancellationTokenSource(RequestEnvelope.DefaultTimeoutMs);
            var begun = pending.BeginAsync(cts.Token).GetAwaiter().GetResult();

            GraphLog.SessionEvent("signInBegun", flowId);
            return Emit(begun with { FlowId = flowId }, GraphJsonContext.Default.BeginResultEnvelope);
        }
        catch (Exception ex)
        {
            // A flow that never issued anything must not be left pinning a polling task.
            DiscardFlow(flowId);
            return EmitError("graph_auth_begin", ex);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "graph_auth_complete")]
    public static IntPtr GraphAuthComplete(long flowId, IntPtr inputJson)
    {
        try
        {
            var pending = PendingFlows.Get(flowId);
            var input = inputJson == IntPtr.Zero ? null : Marshal.PtrToStringUTF8(inputJson);

            // The one long block by design: it waits for a human, so the window is the sign-in
            // window rather than the request timeout (§6.3).
            using var cts = new CancellationTokenSource(DelegatedSignIn.Window);
            var credential = pending.CompleteAsync(input, cts.Token).GetAwaiter().GetResult();

            var session = GraphSession.Create(credential, pending.Scopes);
            var handle = Sessions.Add(session);

            DiscardFlow(flowId);
            GraphLog.SessionEvent("sessionCreated", handle);

            return Emit(
                new SessionEnvelope { Ok = true, Handle = handle, CoreVersion = CoreVersion },
                GraphJsonContext.Default.SessionEnvelope);
        }
        catch (Exception ex)
        {
            return EmitError("graph_auth_complete", ex);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "graph_auth_cancel")]
    public static IntPtr GraphAuthCancel(long flowId)
    {
        try
        {
            PendingFlows.Remove(flowId).DisposeAsync().AsTask().GetAwaiter().GetResult();
            GraphLog.SessionEvent("signInCancelled", flowId);

            return Emit(
                new SessionEnvelope { Ok = true, Handle = flowId },
                GraphJsonContext.Default.SessionEnvelope);
        }
        catch (Exception ex)
        {
            return EmitError("graph_auth_cancel", ex);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "graph_client_close")]
    public static IntPtr GraphClientClose(long handle)
    {
        try
        {
            Sessions.Remove(handle).DisposeAsync().AsTask().GetAwaiter().GetResult();
            GraphLog.SessionEvent("sessionClosed", handle);

            return Emit(
                new SessionEnvelope { Ok = true, Handle = handle },
                GraphJsonContext.Default.SessionEnvelope);
        }
        catch (Exception ex)
        {
            return EmitError("graph_client_close", ex);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "graph_request")]
    public static IntPtr GraphRequest(long handle, IntPtr requestJson)
    {
        try
        {
            var request = Parse(requestJson, GraphJsonContext.Default.RequestEnvelope);
            var session = Sessions.Get(handle);

            using var cts = new CancellationTokenSource(
                request.TimeoutMs ?? RequestEnvelope.DefaultTimeoutMs);

            var response = session.Executor
                .ExecuteAsync(request, cts.Token)
                .GetAwaiter()
                .GetResult();

            return Emit(response, GraphJsonContext.Default.ResponseEnvelope);
        }
        catch (Exception ex)
        {
            return EmitError("graph_request", ex);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "graph_download")]
    public static IntPtr GraphDownload(long handle, IntPtr requestJson) =>
        RunFileOperation("graph_download", handle, requestJson, r => new DownloadOperation(r));

    [UnmanagedCallersOnly(EntryPoint = "graph_upload")]
    public static IntPtr GraphUpload(long handle, IntPtr requestJson) =>
        RunFileOperation("graph_upload", handle, requestJson, r => new UploadOperation(r));

    [UnmanagedCallersOnly(EntryPoint = "graph_free")]
    public static void GraphFree(IntPtr pointer)
    {
        if (pointer != IntPtr.Zero)
        {
            Marshal.FreeCoTaskMem(pointer);
        }
    }

    /// <summary>
    /// Download and upload differ from each other only in which operation they build, so they
    /// share everything else — parsing, the handle lookup, the timeout and the mandatory catch.
    /// </summary>
    private static IntPtr RunFileOperation(
        string entryPoint, long handle, IntPtr requestJson, Func<RequestEnvelope, GraphOperation> build)
    {
        try
        {
            var request = Parse(requestJson, GraphJsonContext.Default.RequestEnvelope);
            var session = Sessions.Get(handle);

            using var cts = new CancellationTokenSource(
                request.TimeoutMs ?? RequestEnvelope.DefaultTimeoutMs);

            var response = session.Executor
                .ExecuteAsync(build(request), cts.Token)
                .GetAwaiter()
                .GetResult();

            return Emit(response, GraphJsonContext.Default.ResponseEnvelope);
        }
        catch (Exception ex)
        {
            return EmitError(entryPoint, ex);
        }
    }

    /// <summary>
    /// A device code expires in about fifteen minutes anyway, so an abandoned sign-in is swept on
    /// the next begin rather than being allowed to pin a background polling task forever (§7.5).
    /// </summary>
    private static void EvictExpiredFlows()
    {
        foreach (var expired in PendingFlows.RemoveWhere(flow => flow.HasExpired))
        {
            Dispose(expired);
        }
    }

    private static void DiscardFlow(long flowId)
    {
        if (flowId == 0)
        {
            return;
        }

        try
        {
            Dispose(PendingFlows.Remove(flowId));
        }
        catch (GraphCoreException)
        {
            // Already gone, which is the outcome we wanted.
        }
    }

    private static void Dispose(PendingAuthentication flow)
    {
        try
        {
            flow.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
        catch (Exception)
        {
            // Tearing down an abandoned sign-in must not replace the error being reported.
        }
    }

    private static T Parse<T>(IntPtr json, JsonTypeInfo<T> typeInfo)
        where T : class
    {
        var text = json == IntPtr.Zero ? null : Marshal.PtrToStringUTF8(json);
        if (string.IsNullOrWhiteSpace(text))
        {
            throw new GraphCoreException("invalidRequest", "the envelope was null or empty");
        }

        return JsonSerializer.Deserialize(text, typeInfo)
            ?? throw new GraphCoreException("invalidRequest", "the envelope deserialised to null");
    }

    private static IntPtr Emit<T>(T value, JsonTypeInfo<T> typeInfo) =>
        Marshal.StringToCoTaskMemUTF8(JsonSerializer.Serialize(value, typeInfo));

    private static IntPtr EmitError(string entryPoint, Exception ex)
    {
        var envelope = ErrorEnvelope.From(ex);

        // The only place a failure that never produced a response can be observed, since the
        // exception is converted here and never reaches the caller as an exception.
        GraphLog.BoundaryFailure(entryPoint, envelope.Error.Code, envelope.Error.Message);

        return Emit(envelope, GraphJsonContext.Default.ErrorEnvelope);
    }
}
