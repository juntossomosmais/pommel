---
paths: "plugins/conditional-rules-plugin/**/*,plugins/**/rules/**/*"
---

# Working on the conditional-rules hook

For authoring rules only (no Python changes), read `CONDITIONAL_RULES.md` instead.

## Keep the file split intact

- Put all logic in `conditional_rules.py`: config loading, validation, evaluation, cache I/O, orchestration, and the `run_entry(event_name, script_name)` shim that wraps stdin/stdout.
- Keep `conditional_rules_pre.py`, `conditional_rules_post.py`, and `conditional_rules_session.py` under ~15 lines each. Prepend the hooks dir to `sys.path`, import `run_entry`, call it with a hard-coded event name. Add no logic.

## When you add behavior

- **New predicate** — add the key to `TRIGGERING_FILE_PREDICATES` or `CONTEXT_ONLY_PREDICATES`, wire a branch in `_validate_predicate` and in `evaluate()`, add tests.
- **New event** — add it to `SUPPORTED_EVENTS`, decide whether it belongs in `TOOL_EVENTS`, add a matching entry script, register it in `hooks.json`, update `CONDITIONAL_RULES.md`.

## Non-negotiables

- Use `plugin_root` to resolve `content_file` and reject paths that escape it. Use the syntactic `_is_safe_rel_path` check (no `..`, no absolute) for `any_file_*` at config load. Never trust raw path strings.
- Keep `content_file` existence checks lazy (in `Rule.resolve_content`, not at config time). Eager checks cause a bootstrapping deadlock when authoring rules that point at files you have not written yet.
- Keep the `fcntl.flock` read-modify-write dance in `append_to_cache`. Do not simplify it to a plain write.
- On `ConfigError` during `PreToolUse`, emit a `deny` decision. On any other event, log to stderr and return `None` — never block non-tool events on config errors.
- Stay on the Python 3 stdlib. Do not add third-party dependencies.
- Treat malformed settings files as "no overrides," never as a hard error. `_read_enabled_plugins_section` swallows `OSError` and `JSONDecodeError` and returns `{}` on every off-shape input — a corrupted `~/.claude/settings.json` must not block tool calls or delete-rule any plugin.

## `enabledPlugins` is the gate, not the registry

Registry records mean "installed," not "enabled." `_load_disabled_plugins` merges `enabledPlugins` from three settings files in lowest-to-highest precedence (`~/.claude/settings.json`, `<project>/.claude/settings.json`, `<project>/.claude/settings.local.json`) and returns the set of plugin keys with an effective `false`. `_discover_from_registry` skips those keys. Plugins absent from every map default to enabled. The `marketplace_dir` fallback path (used only when the registry is missing) does **not** consult `enabledPlugins` — it's an authoring-only path.

"Installed" is also scope-bound: `_record_applies_to_project` gates each record before its `installPath` is used. `user` and `managed` records apply everywhere; `project` and `local` records apply only when their `projectPath` is absolute and resolves to the current `project_root` (a relative `projectPath` would resolve against the hook's cwd — the project dir — and match everywhere, so it is rejected). A record with a missing or unrecognized `scope` is skipped and logged to stderr once by `_discover_from_registry`; the routine "belongs to another repo" skip is never logged, since it happens on every event. Unlike the malformed-settings handling above, this gate fails **closed** — skipping a source only means no rules are injected, while loading another project's rules is the bug it exists to prevent. The default-to-enabled rule of `enabledPlugins` cannot cover this: a project-scope plugin is absent from every map yet must not apply outside its own repo. Because scope is bound to the exact path, a fresh clone or git worktree of the same repo has no record of its own and needs its own `/plugin install` before project/local-scope rules load there.

## Reformat rules.json after every edit

Run the bundled formatter any time you add, remove, or edit a rule — it preserves the inline-leaf layout the file ships with (single-key primitive-only predicates on one line, nested structures expanded):

```bash
python3 plugins/conditional-rules-plugin/hooks/conditional_rules/format_rules.py <path/to/rules.json>
```

For example, to reformat the backend-csharp-plugin's rules:

```bash
python3 plugins/conditional-rules-plugin/hooks/conditional_rules/format_rules.py plugins/backend-csharp-plugin/rules/rules.json
```

Do not use `python3 -m json.tool` on `rules.json` — it expands every object uniformly and produces an unreadable layout. If you need to adjust the formatter, edit `format_rules.py` in place; keep it stdlib-only and self-contained.

## Run the tests

The test suite is split across six domain-focused files plus a shared helpers module:

| File | Domain |
|------|--------|
| `test_conditional_rules_for_engine.py` | Core engine — `evaluate()` / `EvalContext`, `normalize_project_relative()`, output builders |
| `test_conditional_rules_for_marketplace.py` | Marketplace plugin system — config validation, field validation, activation criteria, handle flows, entry scripts, end-to-end |
| `test_conditional_rules_for_installed_plugins_registry.py` | Registry-based discovery (`installed_plugins.json`), version drift, malformed registry handling |
| `test_conditional_rules_for_enabled_plugins_filter.py` | `enabledPlugins` settings filter — disabled plugins are skipped, scope precedence, malformed-tolerant |
| `test_conditional_rules_for_project.py` | Project-level rules (`.claude/conditional_rules/rules.json`) |
| `test_conditional_rules_for_marketplace_and_project.py` | Interaction between both rule sources |
| `_test_helpers.py` | Shared fixtures (`create_structure`, `_rule`, `_RepoFixture`, `_RepoTestCase`, `_EntryScriptRunner`) — not a test file |

Run the full suite from the repo root:

```bash
cd plugins/conditional-rules-plugin/hooks/conditional_rules && python3 -m unittest discover -v
```

Run a single test file or class:

```bash
cd plugins/conditional-rules-plugin/hooks/conditional_rules && python3 -m unittest test_conditional_rules_for_marketplace.OutputShapeTests -v
cd plugins/conditional-rules-plugin/hooks/conditional_rules && python3 -m unittest test_conditional_rules_for_marketplace.OutputShapeTests.test_system_message_lists_each_rule_source -v
```

Run the full suite after every change to `conditional_rules.py`, and get it green before calling the task done. Add at least one test for every new branch (predicate, combinator, event, validation error). Reuse `create_structure`, `_RepoFixture`, and `_rule()` from `_test_helpers.py` instead of hand-rolling fixtures. When you touch `rules.schema.json`, keep `test_schema_file_parses_as_valid_json` in sync.

## Simulate a hook event from the shell

Export both env vars Claude Code hands to hook subprocesses — the hook reads them to locate the plugin and the user's project — plus the two overrides that keep the test away from your real installed plugins:

```bash
export CLAUDE_PLUGIN_ROOT="$PWD/plugins/conditional-rules-plugin"
export CLAUDE_PROJECT_DIR="/path/to/a/test/project"
export CLAUDE_INSTALLED_PLUGINS_FILE="/tmp/does-not-exist.json"   # don't read your real installed-plugins registry
export CLAUDE_MARKETPLACE_DIR="$PWD"                              # discover this repo's rule packs instead
```

**PreToolUse** — an `Edit` on a file that matches a rule:

```bash
echo '{
  "session_id": "smoke-test-1",
  "cwd": "'"$CLAUDE_PROJECT_DIR"'",
  "tool_name": "Edit",
  "tool_input": {"file_path": "'"$CLAUDE_PROJECT_DIR"'/src/SomeFile.cs"}
}' | python3 "$CLAUDE_PLUGIN_ROOT/hooks/conditional_rules/conditional_rules_pre.py"
```

**PostToolUse** — same payload shape, different script:

```bash
echo '{
  "session_id": "smoke-test-1",
  "cwd": "'"$CLAUDE_PROJECT_DIR"'",
  "tool_name": "Write",
  "tool_input": {"file_path": "'"$CLAUDE_PROJECT_DIR"'/src/SomeFile.cs"}
}' | python3 "$CLAUDE_PLUGIN_ROOT/hooks/conditional_rules/conditional_rules_post.py"
```

**SessionStart** — no `tool_name`, no `tool_input`:

```bash
echo '{
  "session_id": "smoke-test-1",
  "cwd": "'"$CLAUDE_PROJECT_DIR"'"
}' | python3 "$CLAUDE_PLUGIN_ROOT/hooks/conditional_rules/conditional_rules_session.py"
```

Payload fields:

- `session_id` (required) — keys the per-session dedup cache at `${CLAUDE_PLUGIN_ROOT}/hooks/conditional_rules/.state/<session_id>.json`. Use a fresh id to fire again; reuse it to exercise dedup.
- `agent_id` (optional) — present in Claude Code's hook input when the call originates inside a subagent. When set, the cache file is keyed as `<session_id>__<agent_id>.json` so subagents get their own dedup scope. Omit to simulate the main session.
- `cwd` (optional) — falls back to `CLAUDE_PROJECT_DIR`, then `Path.cwd()`.
- `tool_name` (required for tool events) — must be `Edit`, `Write`, or `Read` to reach any rule.
- `tool_input.file_path` (optional, absolute path) — omit to exercise `any_file_*` predicates only.

Reading the output:

- Empty stdout → no rule fired. Delete the cache file and retry.
- JSON with `hookSpecificOutput.additionalContext` → rule matched.
- JSON with `permissionDecision: "deny"` → `rules.json` has a `ConfigError`. Read `permissionDecisionReason` for the location.

Inspect or reset the cache directly when debugging dedup:

```bash
cat "$CLAUDE_PLUGIN_ROOT/hooks/conditional_rules/.state/smoke-test-1.json"
rm  "$CLAUDE_PLUGIN_ROOT/hooks/conditional_rules/.state/smoke-test-1.json"   # force re-fire
```
