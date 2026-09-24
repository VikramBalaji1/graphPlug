using System.Collections.Concurrent;
using MicrosoftGraph.Models;

namespace MicrosoftGraph.Interop;

/// <summary>
/// Maps opaque <c>int64</c> handles to managed objects across the ABI. Backed by a
/// <see cref="ConcurrentDictionary{TKey,TValue}"/>, so a handle is safe to use from several threads.
/// </summary>
internal sealed class HandleRegistry<T>
    where T : notnull
{
    private readonly ConcurrentDictionary<long, T> _items = new();
    private long _lastHandle;

    public long Add(T item)
    {
        var handle = Interlocked.Increment(ref _lastHandle);
        _items[handle] = item;
        return handle;
    }

    public T Get(long handle) =>
        _items.TryGetValue(handle, out var item)
            ? item
            : throw new GraphCoreException("invalidHandle", $"handle {handle} is unknown or closed");

    /// <summary>Removes and returns the item, or throws <c>invalidHandle</c> if it is already gone.</summary>
    public T Remove(long handle) =>
        _items.TryRemove(handle, out var item)
            ? item
            : throw new GraphCoreException("invalidHandle", $"handle {handle} is unknown or closed");

    /// <summary>
    /// Removes and returns everything matching, so the caller can dispose it. Swept lazily on the
    /// next use rather than by a timer: a shared library should not own a background thread.
    /// </summary>
    public List<T> RemoveWhere(Func<T, bool> predicate)
    {
        var removed = new List<T>();
        foreach (var (handle, item) in _items)
        {
            if (predicate(item) && _items.TryRemove(handle, out var taken))
            {
                removed.Add(taken);
            }
        }

        return removed;
    }
}
