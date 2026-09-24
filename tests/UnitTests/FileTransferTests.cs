using System.Net;
using MicrosoftGraph.Graph.Operations;
using MicrosoftGraph.Graph.Upload;
using MicrosoftGraph.Models;
using MicrosoftGraph.Models.Envelopes;

namespace UnitTests;

public class FileTransferTests : IDisposable
{
    private readonly string _workspace =
        Directory.CreateTempSubdirectory("msgraph-file-tests").FullName;

    public void Dispose()
    {
        try
        {
            Directory.Delete(_workspace, recursive: true);
        }
        catch (IOException)
        {
            // A temp directory that outlives the test run is not worth failing over.
        }

        GC.SuppressFinalize(this);
    }

    private string Path(string name) => System.IO.Path.Combine(_workspace, name);

    private string WriteFile(string name, long bytes)
    {
        var path = Path(name);
        using var file = File.Create(path);
        file.SetLength(bytes);
        return path;
    }

    // ── upload strategy selection (§8.7) ─────────────────────────────────────

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(UploadOperation.ChunkedThresholdBytes - 1)]
    public void Below_4_MiB_a_single_put_is_used(long size)
    {
        var file = new FileInfo(WriteFile($"small-{size}.bin", size));

        UploadOperation.SelectStrategy(file).Should().BeOfType<SimpleUploadStrategy>();
    }

    [Theory]
    [InlineData(UploadOperation.ChunkedThresholdBytes)]
    [InlineData(UploadOperation.ChunkedThresholdBytes + 1)]
    public void At_4_MiB_and_above_an_upload_session_is_used(long size)
    {
        var file = new FileInfo(WriteFile($"large-{size}.bin", size));

        UploadOperation.SelectStrategy(file).Should().BeOfType<ChunkedUploadStrategy>();
    }

    [Fact]
    public void The_threshold_is_exactly_4_MiB()
    {
        UploadOperation.ChunkedThresholdBytes.Should().Be(4 * 1024 * 1024);
    }

    [Fact]
    public void The_chunk_size_is_a_multiple_of_320_KiB()
    {
        // Graph rejects an upload session chunk that is not.
        ChunkedUploadStrategy.ChunkAlignment.Should().Be(320 * 1024);
        (ChunkedUploadStrategy.ChunkSize % ChunkedUploadStrategy.ChunkAlignment).Should().Be(0);
        ChunkedUploadStrategy.ChunkSize.Should().Be(10 * 1024 * 1024);
    }

    [Theory]
    [InlineData("/me/drive/root:/big.zip:/content", "/me/drive/root:/big.zip:/createUploadSession")]
    [InlineData("/me/drive/items/01ABC/content", "/me/drive/items/01ABC/createUploadSession")]
    [InlineData("/me/drive/root:/big.zip:/createUploadSession", "/me/drive/root:/big.zip:/createUploadSession")]
    public void A_content_path_becomes_an_upload_session_path(string given, string expected)
    {
        // The caller writes one path for both strategies and never learns which one ran.
        ChunkedUploadStrategy.ToSessionPath(given).Should().Be(expected);
    }

    [Fact]
    public void An_upload_of_a_file_that_does_not_exist_is_rejected_by_name()
    {
        var request = new RequestEnvelope
        {
            Method = "PUT",
            Path = "/me/drive/root:/x:/content",
            SourcePath = Path("absent.bin"),
        };

        FluentActions.Invoking(() => new UploadOperation(request))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public void An_upload_without_a_source_path_is_rejected()
    {
        var request = new RequestEnvelope { Method = "PUT", Path = "/me/drive/root:/x:/content" };

        FluentActions.Invoking(() => new UploadOperation(request))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public async Task A_small_upload_sends_the_file_as_a_single_put()
    {
        var source = WriteFile("small.bin", 1024);
        var transport = RecordingTransport.Returning(
            HttpStatusCode.Created, """{ "id": "01ABC", "name": "small.bin" }""");

        var response = await OperationRunner.RunAsync(
            new UploadOperation(new RequestEnvelope
            {
                Method = "PUT",
                Path = "/me/drive/root:/small.bin:/content",
                SourcePath = source,
            }),
            transport);

        response.Ok.Should().BeTrue();
        response.Status.Should().Be(201);
        response.BytesSent.Should().Be(1024);
        response.Body!["id"]!.GetValue<string>().Should().Be("01ABC");
        transport.RequestUri!.AbsolutePath.Should().EndWith(":/content");
    }

    [Fact]
    public async Task A_failed_upload_produces_the_same_error_shape_as_any_other_failure()
    {
        var source = WriteFile("small.bin", 16);
        var transport = RecordingTransport.Returning(
            HttpStatusCode.Forbidden,
            """{ "error": { "code": "accessDenied", "message": "no write access" } }""");

        var response = await OperationRunner.RunAsync(
            new UploadOperation(new RequestEnvelope
            {
                Method = "PUT",
                Path = "/me/drive/root:/small.bin:/content",
                SourcePath = source,
            }),
            transport);

        response.Ok.Should().BeFalse();
        response.Status.Should().Be(403);
        response.Error!.Code.Should().Be("accessDenied");
    }

    // ── downloads (§8.6) ─────────────────────────────────────────────────────

    [Fact]
    public async Task A_download_streams_to_disk_and_reports_what_it_wrote()
    {
        var destination = Path("out.bin");
        var payload = new string('x', 5000);
        var transport = new RecordingTransport(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(payload),
        });

        var response = await OperationRunner.RunAsync(
            new DownloadOperation(new RequestEnvelope
            {
                Method = "GET",
                Path = "/me/drive/items/01ABC/content",
                DestPath = destination,
            }),
            transport);

        response.Ok.Should().BeTrue();
        response.BytesWritten.Should().Be(5000);
        response.DestPath.Should().Be(destination);
        response.Body.Should().BeNull("the bytes are on disk, not in the envelope");
        new FileInfo(destination).Length.Should().Be(5000);
    }

    [Fact]
    public async Task A_failed_download_leaves_no_file_at_the_destination()
    {
        var destination = Path("never.bin");
        var transport = RecordingTransport.Returning(
            HttpStatusCode.NotFound, """{ "error": { "code": "itemNotFound" } }""");

        var response = await OperationRunner.RunAsync(
            new DownloadOperation(new RequestEnvelope
            {
                Method = "GET",
                Path = "/me/drive/items/nope/content",
                DestPath = destination,
            }),
            transport);

        response.Ok.Should().BeFalse();
        response.Error!.Code.Should().Be("itemNotFound");
        File.Exists(destination).Should().BeFalse();
    }

    [Fact]
    public async Task A_download_interrupted_mid_transfer_leaves_nothing_behind()
    {
        var destination = Path("partial.bin");
        var transport = new RecordingTransport(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StreamContent(new FailingStream()),
        });

        var act = () => OperationRunner.RunAsync(
            new DownloadOperation(new RequestEnvelope
            {
                Method = "GET",
                Path = "/me/drive/items/01ABC/content",
                DestPath = destination,
            }),
            transport);

        await act.Should().ThrowAsync<IOException>();

        File.Exists(destination).Should().BeFalse("the rename only happens on success");
        Directory.GetFiles(_workspace, "*.partial").Should().BeEmpty("the temp file is cleaned up");
    }

    [Fact]
    public void A_download_without_a_destination_is_rejected()
    {
        FluentActions
            .Invoking(() => new DownloadOperation(new RequestEnvelope
            {
                Method = "GET",
                Path = "/me/drive/items/01ABC/content",
            }))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidRequest");
    }

    [Fact]
    public void A_destination_directory_that_does_not_exist_is_reported_not_created()
    {
        var destination = Path(System.IO.Path.Combine("absent-dir", "out.bin"));

        FluentActions
            .Invoking(() => new DownloadOperation(new RequestEnvelope
            {
                Method = "GET",
                Path = "/x/content",
                DestPath = destination,
            }))
            .Should().Throw<GraphCoreException>()
            .Which.Message.Should().Contain("does not exist");

        Directory.Exists(Path("absent-dir")).Should().BeFalse("the core does not create directories");
    }

    /// <summary>Fails part way through, the way a dropped connection does.</summary>
    private sealed class FailingStream : Stream
    {
        private int _served;

        public override bool CanRead => true;

        public override bool CanSeek => false;

        public override bool CanWrite => false;

        public override long Length => throw new NotSupportedException();

        public override long Position
        {
            get => _served;
            set => throw new NotSupportedException();
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            if (_served >= 1024)
            {
                throw new IOException("the connection was reset");
            }

            _served += count;
            return count;
        }

        public override void Flush()
        {
        }

        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();

        public override void SetLength(long value) => throw new NotSupportedException();

        public override void Write(byte[] buffer, int offset, int count) =>
            throw new NotSupportedException();
    }
}
