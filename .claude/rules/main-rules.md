# SDLC rules

Imperative guidance for working on this repository. Follow these end-to-end on every change.

## Testing

- Run the full engine suite before declaring a change complete:
    ```bash
    docker compose run --remove-orphans --rm integration-tests
    ```
- Run the end-to-end smoke test whenever anything under `plugins/` changes:
    ```bash
    docker compose run --remove-orphans --rm integration-tests ./scripts/smoke-test.sh
    ```

## Lint & format

- Run after the implementation is complete (no need to re-run tests after):
    ```bash
    docker compose run --remove-orphans --rm lint-formatter
    ```
- `lint-formatter` rewrites files in place and exits non-zero if it changed anything or a check failed. Per `ruff.toml`, markdown files are excluded (rule bodies are content, not code) and `BLE001` is deliberately ignored — the engine's blanket `except Exception` handlers are what keep it from ever crashing a tool call. Do not "fix" them.

## Rules files

- After adding, removing, or editing any `rules.json` entry, reformat it:
    ```bash
    python3 plugins/conditional-rules-plugin/hooks/conditional_rules/format_rules.py <path/to/rules.json>
    ```

## Validation

- Before finishing, validate the marketplace and every plugin manifest:
    ```bash
    claude plugin validate .
    for plugin in plugins/*/; do claude plugin validate "$plugin"; done
    ```

## Documentation

- Each plugin has its own changelog at `plugins/<plugin>/CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format) — there is no repo-wide changelog. In the same commit as a change to a plugin's content, add or update that plugin's entry under the active `## [X.Y.Z]` heading using the `Added` / `Changed` / `Fixed` / `Removed` subsections. Skip it for repo-tooling edits (workflows, Docker/compose, `scripts/`, root-level docs) — those belong to no plugin.
- Update `README.md` when installation steps, requirements, or the plugin catalog change.
- `docs/conditional-rules-portability.md` is a dated snapshot — re-verify against primary sources before editing any verdict.
- Do **not** create new top-level docs (`*.md`) unless explicitly asked.

## Versioning & commits

- When a change to a plugin is ready to ship, bump `version` in `plugins/<plugin>/.claude-plugin/plugin.json` following [semver](https://semver.org/): patch for fixes, minor for additive changes, major for breaking changes. Add a matching `## [X.Y.Z] - YYYY-MM-DD` heading to that plugin's `CHANGELOG.md`, and commit both alongside the change (e.g. `chore(backend-csharp-plugin): bump version to 0.1.1`).
- Use Conventional Commits.
- Distribution is manual: users add the marketplace and install plugins themselves (`/plugin marketplace add juntossomosmais/pommel`). There is no release automation and no tag-driven publishing.
