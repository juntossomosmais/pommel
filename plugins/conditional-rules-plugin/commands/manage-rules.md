---
description: Manage rules — conventional or conditional, trigger design, tag-pinned citations, smoke test
---

# Rules

## Classify first, then edit

**Conventional** (native Claude Code memory) — skip the conditional docs entirely and edit the file:

- the target is any `CLAUDE.md`, or any path under `.claude/rules/`
- the user says "conventional"

**Conditional** — read `${CLAUDE_PLUGIN_ROOT}/hooks/conditional_rules/CONDITIONAL_RULES.md` for predicate / combinator / event mechanics before writing. Not found: ask the user where it lives. Project-level conditional rules live in `.claude/conditional_rules/rules.json` (always applied for that repo; `activation_criteria` is ignored there); plugin rule packs keep theirs in `rules/rules.json`.

**Neither is stated and no path gives it away** — ask which. For conventional, the `claude-code-guide` subagent (if available) covers best practices.

## Writing a rule

- A rule must state things as imperative (describe what the developer should do) and not as descriptive (describe what the code does).
- Rules should be concise and focused on specific conditions or patterns to enforce.
- Rules should be easily understandable and maintainable by developers.
- Rules should be designed to prevent common mistakes and ensure code quality.
- Rules should be short—very short—and as pragmatic as possible.
- Rules should not reference other rules because they are loaded progressively.
- When a rule content requires an external link, prefer GitHub repositories links.

## Pin every citation link

`/blob/main/...` links rot silently. Pin to a stable ref:

- **Semver tags** → `/blob/<vX.Y.Z>/path`. Find latest:
  ```bash
  curl -s https://api.github.com/repos/<owner>/<repo>/releases/latest | python3 -c "import sys, json; print(json.load(sys.stdin)['tag_name'])"
  ```
- **Date tags** (e.g. `opentelemetry.io`) → `/blob/<YYYY.MM>/path`. Find latest:
  ```bash
  curl -s 'https://api.github.com/repos/<owner>/<repo>/tags?per_page=5' | python3 -c "import sys, json; print([t['name'] for t in json.load(sys.stdin)])"
  ```
- **Neither** → commit SHA. Find latest for a specific file:
  ```bash
  curl -s 'https://api.github.com/repos/<owner>/<repo>/commits?path=<filepath>&per_page=1' | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['sha'])"
  ```

After pinning, verify the cited text still exists at the chosen ref (with the octocode MCP, `ghGetFileContent` with `branch: "<ref>"`, `matchString: "<quoted phrase>"`; otherwise `curl` the pinned raw.githubusercontent.com URL and grep for the quoted phrase).

## Smoke test

After writing or editing a conditional rule, exercise it locally with the "Testing your rules locally" recipes in `${CLAUDE_PLUGIN_ROOT}/hooks/conditional_rules/CONDITIONAL_RULES.md`: verify the rule fires (JSON output with `additionalContext`) and that it dedups on a second run with the same `session_id`.
