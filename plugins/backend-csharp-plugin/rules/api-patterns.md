# API Patterns

## Rules

- All API endpoints should require authentication/authorization.
- Extend `BaseController<TDto, TEntity, TPrimaryKey, TContext>` from `NDjango.RestFramework.Base`.
- Inject the matching `Serializer<>` and `AppDbContext` through the constructor and forward them to `base(serializer, context, logger, new PageNumberPagination<TEntity>())`. Use `PageNumberPagination<TEntity>` unless the resource has a specific reason to paginate differently.
- **Configure the controller in the constructor.** That is the only place to set `Query` (a `.AsNoTracking()` read query, with `.Include(...)` when the response needs related data — `Query` is the default queryset for every read path), `Filters`, `AllowedFields`, and `ActionOptions` (e.g., `new ActionOptions { AllowPut = false }` to disable an action, as in `SalesController`).
- **Action bodies hold no business logic.** No `if`-driven branching, no `DbContext` writes (`Add` / `Update` / `Remove` / `ExecuteUpdate*` / `ExecuteDelete*`), no manual mapping, no validation. Custom action methods (`[HttpPost("{id}/complete")]` and similar) are allowed only when the framework's CRUD shape doesn't fit — they delegate writes to the serializer or to a Hangfire job, never to `DbContext` directly. Read-only short-circuit guards (`.AsNoTracking()` existence checks) are fine; business writes are not.
- **Override the controller seams only for request-shaped concerns.** Use `PerformCreateAsync` / `PerformUpdateAsync` / `PerformPartialUpdateAsync` / `PerformDestroyAsync` for HTTP-scoped side effects — audit metadata derived from `HttpContext`, request-scoped tracing, response-shaping — by mutating `data` / `instance` and then delegating to `base`. Use `ValidateDestroyAsync` to short-circuit DELETE with a 400 by populating `errors` (e.g., "can't delete the main address while siblings exist"); the framework does not wrap this hook in a transaction, so never perform side effects here. Reach into the serializer for shared checks (e.g., `_serializer.CanDeleteAsync(...)`). If the same logic is needed by consumers or jobs, put it in the serializer's `CreateAsync` / `UpdateAsync` / `PartialUpdateAsync` / `DestroyAsync` instead.
- Do not create `Service` classes for new features. All domain logic for an entity — single-caller or shared — lives in its `Serializer<>`.

## API versioning (mandatory)

Both the **namespace** and the **route** must include the version:

```csharp
namespace DotNetTemplate.Controllers.V1;

[Route("v1/addresses")]
public class AddressesController : BaseController<AddressDto, Address, int, AppDbContext>
```

- Use `V{n}` in the namespace: `DotNetTemplate.Controllers.V{n}`.
- Use `v{n}/{resource}` (lowercase) in the route attribute.
- Never create unversioned controllers.
- Bump `V{n}` → `V{n+1}` only for breaking wire-shape changes (rename, type change, removed field, semantics change). Adding an optional field is non-breaking — stay on `V{n}`. The controller version mirrors the serializer version it uses.

## Examples

### Plain controller — base behavior is enough

```csharp
using DotNetTemplate.Serializers.V1;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using NDjango.RestFramework.Base;
using NDjango.RestFramework.Paginations;

namespace DotNetTemplate.Controllers.V1;

[Route("v1/stores")]
public class StoresController : BaseController<StoreDto, Store, int, AppDbContext>
{
    private readonly AppDbContext _context;

    public StoresController(
        StoreSerializer serializer,
        AppDbContext context,
        ILogger<Store> logger)
        : base(serializer, context, logger, new PageNumberPagination<Store>())
    {
        _context = context;
        Query = _context.Stores
            .Include(s => s.Addresses)
            .AsNoTracking();
    }
}
```