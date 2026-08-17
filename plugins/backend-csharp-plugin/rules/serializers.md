# Serializers

NDjango.RestFramework `Serializer<TOrigin, TDestination, TPrimaryKey, TContext>` classes own DTO validation, CRUD, and side effects (CAP publish, Hangfire enqueue, etc.) for a single wire-contract version. Controllers, Consumers, and Jobs share the same serializer instance — this is by design, not a smell.

## Layout

```
src/Serializers/V1/
├── AddressSerializer.cs   ← AddressDto + AddressSerializer
└── StoreSerializer.cs     ← StoreDto + StoreAddressDto + StoreSerializer
```

- One file per entity, under `src/Serializers/V{n}/`. The version mirrors the route version of the controller that uses it.
- Each file contains the DTO(s) plus the `Serializer<>` subclass.
- Namespace: `DotNetTemplate.Serializers.V{n}`.

## Rules

- **One serializer per (entity, wire version).** `Serializer<TOrigin, ...>` binds to a single DTO shape — a new wire shape means a new serializer in `V{n+1}`.
- **V2 is parallel, not a replacement.** When V2 ships, V1 stays — its callers (controllers, consumers, jobs still on the V1 wire) keep using it until they migrate.
- **Register once, share everywhere.** `services.AddScoped<AddressSerializer>()` — the same registration backs Controllers, Consumers, and Jobs.
- **Business logic lives in the serializer — never in the controller.** Controllers extending `BaseController<>` are wiring only: inject the serializer, set `Query`, delegate to `base`. No `if`-driven branching, no `DbContext` writes (`Add` / `Update` / `Remove` / `ExecuteUpdate*` / `ExecuteDelete*`), no validation, no mapping, no side effects in a controller action. Route every write through the serializer override so the same rule applies to controllers, consumers, and jobs.
- **Per-field rules go in `Validate{Property}Async`.** One hook per DTO property; the framework auto-discovers it by name match. The hook may mutate and return the normalized value — the framework writes it back into the DTO (and into the PATCH JSON). Use it for single-field shape rules and FK-existence checks (e.g., strip non-digits from CEP; verify `StoreId` exists on Create). Do not write `AbstractValidator<T>` classes for fields the serializer already validates.
- **Cross-field rules go in the `ValidateAsync` override.** Use it when a rule reads more than one field (e.g., "can't unset the only main address"). It only runs after per-field hooks added no errors. Branch on `context.IsCreate` / `IsUpdate` / `IsPartialUpdate`; on PATCH, use `context.IsSet(nameof(Dto.Field))` to distinguish "absent" from "sent as the type's default".
- **Validation hooks are read-only.** Inside `Validate{Property}Async` and `ValidateAsync`, always query with `.AsNoTracking()`. Never call `SaveChangesAsync`, `Add`, `Update`, `Remove`, `ExecuteUpdate*`, or `ExecuteDelete*` — those belong to the CRUD overrides. EF Core's change tracker can persist prematurely if validation mutates tracked entities.
- **Persistence and side effects go in the CRUD overrides.** Override `CreateAsync` / `UpdateAsync` / `PartialUpdateAsync` / `DestroyAsync` for EF writes, the transaction wrapper (`BeginTransactionAsync(_capPublisher, autoCommit: false)`), CAP publish, Hangfire enqueue, sibling demotion, audit, and anything else with write-time effect. Callers never re-do any of this.

## When to bump the version

Bump `V{n}` → `V{n+1}` only when the **DTO wire shape** breaks: field rename, type change, removed field, or semantics change existing clients can't tolerate. Adding an optional field is non-breaking — keep `V{n}`.
