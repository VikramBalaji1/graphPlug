using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using MicrosoftGraph.Graph;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Graph.Upload;
using MicrosoftGraph.Models.Envelopes;

namespace IntegrationTests;

/// <summary>
/// A file larger than 4 MiB goes up through an upload session and comes back down, and the
/// checksums match (§15 milestone 5).
/// </summary>
public class FileRoundTripTests : IDisposable
{
    private readonly string _workspace =
        Directory.CreateTempSubdirectory("msgraph-roundtrip").FullName;

    public void Dispose()
    {
        try
        {
            Directory.Delete(_workspace, recursive: true);
        }
        catch (IOException)
        {
            // Not worth failing a passing test over.
        }

        GC.SuppressFinalize(this);
    }

    /// <summary>
    /// A miniature drive: it honours createUploadSession, assembles ranged PUTs, and serves the
    /// assembled bytes back on download.
    /// </summary>
    private sealed class ScriptedDrive : HttpMessageHandler
    {
        private readonly MemoryStream _stored = new();

        public List<long> ChunkLengths { get; } = [];

        public List<ContentRangeHeaderValue> Ranges { get; } = [];

        public byte[] Stored => _stored.ToArray();

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;

            HttpResponseMessage response;
            if (path.EndsWith("createUploadSession", StringComparison.Ordinal))
            {
                response = Json(HttpStatusCode.OK, new JsonObject
                {
                    ["uploadUrl"] = "https://contoso.sharepoint.com/_api/upload/session-1",
                    ["expirationDateTime"] = DateTimeOffset.UtcNow.AddHours(1).ToString("O"),
                });
            }
            else if (path.Contains("/upload/session-", StringComparison.Ordinal))
            {
                response = await AcceptChunkAsync(request, cancellationToken);
            }
            else if (path.EndsWith("/content", StringComparison.Ordinal))
            {
                response = new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new ByteArrayContent(Stored),
                };
            }
            else
            {
                response = Json(HttpStatusCode.OK, new JsonObject { ["id"] = "01ABC" });
            }

            response.RequestMessage = request;
            return response;
        }

        private async Task<HttpResponseMessage> AcceptChunkAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var range = request.Content!.Headers.ContentRange!;
            Ranges.Add(range);

            var bytes = await request.Content.ReadAsByteArrayAsync(cancellationToken);
            ChunkLengths.Add(bytes.Length);

            _stored.Seek(range.From!.Value, SeekOrigin.Begin);
            await _stored.WriteAsync(bytes, cancellationToken);

            var complete = range.To!.Value + 1 >= range.Length!.Value;

            return complete
                ? Json(HttpStatusCode.Created, new JsonObject
                {
                    ["id"] = "01ABC",
                    ["name"] = "big.bin",
                    ["size"] = range.Length!.Value,
                })
                : Json(HttpStatusCode.Accepted, new JsonObject
                {
                    ["nextExpectedRanges"] = new JsonArray($"{range.To!.Value + 1}-"),
                });
        }

        private static HttpResponseMessage Json(HttpStatusCode status, JsonObject body) =>
            new(status)
            {
                Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
            };
    }

    private static string Sha256(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes));

    private static string Sha256OfFile(string path) => Sha256(File.ReadAllBytes(path));

    private string WriteRandomFile(string name, int bytes)
    {
        var path = Path.Combine(_workspace, name);
        File.WriteAllBytes(path, RandomNumberGenerator.GetBytes(bytes));
        return path;
    }

    private static async Task<ResponseEnvelope> RunAsync(
        GraphOperation operation, HttpMessageHandler transport)
    {
        await using var session = GraphSession.Create(
            new FakeTokenCredential(), ["https://graph.microsoft.com/.default"],
            finalHandler: transport);

        return await session.Executor.ExecuteAsync(operation, CancellationToken.None);
    }

    [Fact]
    public async Task A_file_over_4_MiB_round_trips_with_a_matching_checksum()
    {
        const int Size = (int)(UploadOperation.ChunkedThresholdBytes * 2) + 12_345;

        var source = WriteRandomFile("big.bin", Size);
        var destination = Path.Combine(_workspace, "big-returned.bin");
        var drive = new ScriptedDrive();

        var uploaded = await RunAsync(
            new UploadOperation(new RequestEnvelope
            {
                Method = "PUT",
                Path = "/me/drive/root:/big.bin:/content",
                SourcePath = source,
            }),
            drive);

        uploaded.Ok.Should().BeTrue();
        uploaded.Status.Should().Be(201);
        uploaded.BytesSent.Should().Be(Size);

        var downloaded = await RunAsync(
            new DownloadOperation(new RequestEnvelope
            {
                Method = "GET",
                Path = "/me/drive/items/01ABC/content",
                DestPath = destination,
            }),
            drive);

        downloaded.Ok.Should().BeTrue();
        downloaded.BytesWritten.Should().Be(Size);

        Sha256OfFile(destination).Should().Be(Sha256OfFile(source));
    }

    [Fact]
    public async Task Every_chunk_but_the_last_is_a_whole_number_of_320_KiB_units()
    {
        const int Size = (int)(UploadOperation.ChunkedThresholdBytes * 3) + 7;

        var source = WriteRandomFile("aligned.bin", Size);
        var drive = new ScriptedDrive();

        await RunAsync(
            new UploadOperation(new RequestEnvelope
            {
                Method = "PUT",
                Path = "/me/drive/root:/aligned.bin:/content",
                SourcePath = source,
            }),
            drive);

        drive.ChunkLengths.Should().NotBeEmpty();

        foreach (var length in drive.ChunkLengths.SkipLast(1))
        {
            (length % ChunkedUploadStrategy.ChunkAlignment).Should().Be(0);
            length.Should().Be(ChunkedUploadStrategy.ChunkSize);
        }

        drive.ChunkLengths.Sum().Should().Be(Size);
    }

    [Fact]
    public async Task Content_ranges_are_contiguous_and_declare_the_whole_size()
    {
        const int Size = (int)(UploadOperation.ChunkedThresholdBytes * 2);

        var source = WriteRandomFile("ranges.bin", Size);
        var drive = new ScriptedDrive();

        await RunAsync(
            new UploadOperation(new RequestEnvelope
            {
                Method = "PUT",
                Path = "/me/drive/root:/ranges.bin:/content",
                SourcePath = source,
            }),
            drive);

        var expectedStart = 0L;
        foreach (var range in drive.Ranges)
        {
            range.Length.Should().Be(Size, "every chunk declares the total size");
            range.From.Should().Be(expectedStart);
            expectedStart = range.To!.Value + 1;
        }

        expectedStart.Should().Be(Size, "the ranges must cover the file exactly once");
    }

    [Fact]
    public async Task A_small_file_round_trips_through_the_single_put_path()
    {
        var source = WriteRandomFile("small.bin", 4096);
        var destination = Path.Combine(_workspace, "small-returned.bin");
        var drive = new ScriptedDrive();

        // The simple strategy PUTs straight at the content path, which this drive stores.
        var transport = new SingleShotDrive();

        var uploaded = await RunAsync(
            new UploadOperation(new RequestEnvelope
            {
                Method = "PUT",
                Path = "/me/drive/root:/small.bin:/content",
                SourcePath = source,
            }),
            transport);

        uploaded.Ok.Should().BeTrue();
        uploaded.BytesSent.Should().Be(4096);
        transport.Ranges.Should().BeEmpty("a small upload needs no upload session");

        var downloaded = await RunAsync(
            new DownloadOperation(new RequestEnvelope
            {
                Method = "GET",
                Path = "/me/drive/items/01ABC/content",
                DestPath = destination,
            }),
            transport);

        downloaded.Ok.Should().BeTrue();
        Sha256OfFile(destination).Should().Be(Sha256OfFile(source));
        _ = drive;
    }

    /// <summary>Stores a single PUT body and serves it back, with no upload session involved.</summary>
    private sealed class SingleShotDrive : HttpMessageHandler
    {
        private byte[] _stored = [];

        public List<ContentRangeHeaderValue> Ranges { get; } = [];

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            if (request.Content?.Headers.ContentRange is { } range)
            {
                Ranges.Add(range);
            }

            HttpResponseMessage response;
            if (request.Method == HttpMethod.Put)
            {
                _stored = await request.Content!.ReadAsByteArrayAsync(cancellationToken);
                response = new HttpResponseMessage(HttpStatusCode.Created)
                {
                    Content = new StringContent(
                        new JsonObject { ["id"] = "01ABC" }.ToJsonString(),
                        Encoding.UTF8,
                        "application/json"),
                };
            }
            else
            {
                response = new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new ByteArrayContent(_stored),
                };
            }

            response.RequestMessage = request;
            return response;
        }
    }
}
