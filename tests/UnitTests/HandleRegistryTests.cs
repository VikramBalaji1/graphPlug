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
        var handles = new System.Collections.Concurrent.ConcurrentBag<long>();

        Parallel.For(0, 500, i =>
        {
            var handle = registry.Add($"session-{i}");
            handles.Add(handle);
            registry.Remove(handle);
        });

        handles.Should().HaveCount(500).And.OnlyHaveUniqueItems();
        foreach (var handle in handles)
        {
            registry.Invoking(r => r.Get(handle)).Should().Throw<GraphCoreException>();
        }
    }
}
