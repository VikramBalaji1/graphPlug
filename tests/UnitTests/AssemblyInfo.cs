// GraphLogTests captures the logger's process-wide writer, so a test class running in parallel
// would write its own lines into that capture. The suite runs in well under a second either way.
[assembly: CollectionBehavior(DisableTestParallelization = true)]
