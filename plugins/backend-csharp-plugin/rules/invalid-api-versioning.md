# Invalid: unversioned API surface

- Version the route: `[Route("v{n}/{resource}")]` — lowercase `v`, version first, no `api/` prefix (e.g. `[Route("v1/addresses")]`).
- Version the namespace: `<RootNamespace>.Controllers.V{n}` (e.g. `namespace DotNetTemplate.Controllers.V1;`), never a flat `…Controllers`.
- Keep the file under `src/Controllers/V{n}/`, aligned with the `Serializer<>` version it uses.
- Bump `V{n}` → `V{n+1}` only for breaking wire-shape changes; adding an optional field stays on `V{n}`.
- Fix every `[Route]` and the namespace in this file, not only the one just written.
