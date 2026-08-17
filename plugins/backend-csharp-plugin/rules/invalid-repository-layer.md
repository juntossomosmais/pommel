# Invalid: repository layer reintroduced

- Do not create Repository classes or interfaces — this project persists through `NDjango.RestFramework` serializers.
- Inject `AppDbContext` directly into the controller, consumer handler, job, or serializer that needs it.
- Put domain logic in the entity's `Serializer<>`: per-field rules in `Validate{Property}Async`, cross-field rules in `ValidateAsync`, persistence and side effects in the CRUD overrides.
- Use `.AsNoTracking()` for reads and `ExecuteUpdateAsync` / `ExecuteDeleteAsync` for bulk writes.
- If the matched type is genuinely not data access, confirm that and leave it.
- Resolve every Repository declaration in this file, not only the one just written.
