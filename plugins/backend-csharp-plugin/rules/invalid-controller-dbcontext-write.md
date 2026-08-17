# Invalid: DbContext write inside a controller

- Remove the persistence call — controllers extending `BaseController<>` are wiring only.
- Move standard CRUD writes into the entity's `Serializer<>` override (`CreateAsync` / `UpdateAsync` / `PartialUpdateAsync` / `DestroyAsync`).
- Keep only HTTP-scoped side effects in the `Perform*Async` seams, mutating `data`/`instance` and delegating to `base`.
- Enqueue long-running work as a Hangfire job (`_backgroundJobs.Enqueue<TJob>(job => job.ExecuteAsync(args))`) and return `Accepted()`.
- Keep read-only queries: `Query = _context.X.Include(…).AsNoTracking()` and `.AsNoTracking()` existence guards are fine.
- Scan the whole controller for other writes, not only the one just written.
