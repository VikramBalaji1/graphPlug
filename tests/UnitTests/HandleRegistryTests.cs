using MicrosoftGraph.Interop;
using MicrosoftGraph.Models;

namespace UnitTests;

public class HandleRegistryTests
{
    [Fact]
    public void Round_trips_an_item_by_handle()
    {
        var registry = new HandleRegistry<string>();
        var handle = registry.Add("session");

        registry.Get(handle).Should().Be("session");
    }

    [Fact]
    public void Issues_distinct_handles()
    {
        var registry = new HandleRegistry<string>();

        registry.Add("a").Should().NotBe(registry.Add("b"));
    }

    [Fact]
    public void An_unknown_handle_raises_invalidHandle()
    {
        FluentActions.Invoking(() => new HandleRegistry<string>().Get(99))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidHandle");
    }

    [Fact]
    public void A_closed_handle_raises_invalidHandle()
    {
        var registry = new HandleRegistry<string>();
        var handle = registry.Add("session");
        registry.Remove(handle);

        FluentActions.Invoking(() => registry.Get(handle))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidHandle");
    }

    [Fact]
    public void Closing_twice_raises_invalidHandle_rather_than_succeeding()
    {
        var registry = new HandleRegistry<string>();
        var handle = registry.Add("session");
        registry.Remove(handle);

        FluentActions.Invoking(() => registry.Remove(handle))
            .Should().Throw<GraphCoreException>()
            .Which.Code.Should().Be("invalidHandle");
    }

    [Fact]
    public void Concurrent_adds_and_removes_leave_nothing_behind()
    {
        var registry = new HandleRegistry<string>();

        Parallel.For(0, 500, i => registry.Remove(registry.Add($"session-{i}")));

        registry.Count.Should().Be(0);
    }
}
