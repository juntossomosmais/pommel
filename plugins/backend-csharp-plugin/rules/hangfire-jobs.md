# Hangfire — Background Jobs

## File structure

Each job lives in a **single file** containing a single class:

1. **Job class** — a plain class with an `ExecuteAsync` method. Hangfire resolves it from DI, so constructor injection works normally.

## Naming conventions

| Concern | Convention | Example |
|---|---|---|
| Job class | `{Domain}{Action}Job` | `TodoItemCompletionJob` |
| Execute method | `ExecuteAsync` (always) | — |
| File name | `{Domain}{Action}Job.cs` | `TodoItemCompletionJob.cs` |

## Rules

- **Business logic goes directly in the ExecuteAsync method.** Do not extract business logic into a service class.
- **Inject `AppDbContext` directly** into job classes.
- **Keep job methods small and focused.** One job = one responsibility.
- **Inject `IBackgroundJobClient` via constructor** in controllers that enqueue jobs. Do not use `[FromServices]` or the static `BackgroundJob` API.
- **Use the typed `Enqueue<T>` overload** so Hangfire resolves the job class from DI:
  ```csharp
  _backgroundJobs.Enqueue<MyJob>(job => job.ExecuteAsync(args));
  ```
- **Job parameters must be serializable.** Pass only primitives or simple DTOs — never EF entities, `DbContext`, `HttpContext`, or anything that holds runtime state.
- **Jobs run outside the HTTP request context.** They have their own DI scope. Do not rely on request-scoped state (`HttpContext`, `ClaimsPrincipal`, etc.).
- **Do not enqueue jobs inside a loop.** Hangfire OSS has no transactional batch API — each `Enqueue` is an independent DB insert with no atomicity across calls. If you need to process a collection, pass the full list to a single job. Hangfire Pro's `BatchJob.StartNew` supports atomic batch enqueueing if that is ever adopted.

## Enqueueing from controllers

The controller delegates execution to the job class:

```csharp
[HttpPost("{id}/complete")]
public async Task<IActionResult> ScheduleCompletionAsync(int id)
{
    var exists = await _context.TodoItems.AnyAsync(t => t.Id == id);
    if (!exists) return NotFound();

    _backgroundJobs.Enqueue<TodoItemCompletionJob>(job => job.ExecuteAsync(id));
    return Accepted();
}
```

Return `Accepted()` (202) to signal the work was scheduled, not completed.

## Configuration

Job classes do not need explicit DI registration — Hangfire's `AspNetCoreJobActivator` resolves them via `ActivatorUtilities`.

## Example

```csharp
using Microsoft.EntityFrameworkCore;

namespace DotNetTemplate.Jobs;

public class TodoItemCompletionJob
{
    private readonly AppDbContext _context;

    public TodoItemCompletionJob(AppDbContext context) => _context = context;

    public async Task ExecuteAsync(int todoItemId)
    {
        await _context.TodoItems
            .Where(t => t.Id == todoItemId)
            .ExecuteUpdateAsync(s => s.SetProperty(t => t.IsComplete, true));
    }
}
```
