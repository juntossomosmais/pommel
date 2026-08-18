# Conditional Rules Portability Report

**Pommel · plugin research · 2026-08-17**

> **Snapshot of 2026-08-17.** Harness capabilities, bugs, and docs referenced here change frequently — re-verify against primary sources before starting a port.
>
> Movement since the snapshot — corrections, filed issues, community reports — is tracked in the [Status timeline](#status-timeline). Matrix verdicts change only on a `docs`-class entry there.

Can the `conditional-rules` engine — condition-gated context injection at tool and session events — be rebuilt on other AI coding harnesses? Seven candidate harnesses — plus Claude Code as the shipping reference — were assessed against fourteen capability rows (twelve capabilities, with tool-event hooks split across pre/post/session). Every assessment was produced by a research agent and then adversarially re-verified by an independent fact-checking agent against primary documentation.

---

## 1. What the plugin needs from a harness

Our plugin's loop on Claude Code: hooks at `PreToolUse`/`PostToolUse` (matcher `Edit|Write|Read`) and `SessionStart` run a Python script that receives JSON on stdin (`session_id`, `agent_id`, `tool_name`, `tool_input.file_path`, `cwd`), evaluates rules from `rules.json` (conditions: path glob/regex on the touched file, content regex on the touched file, arbitrary-project-file existence/content checks, combined with `all_of`/`any_of`/`not`), and prints JSON whose `hookSpecificOutput.additionalContext` injects matching rule text into the model context. Each rule injects once per session (dedup cache keyed by `session_id`+`agent_id`). Config errors deny the tool call with a reason. Rules ship inside marketplace plugins discovered from the installed-plugins registry.

The capabilities that support this, used as matrix rows:

| ID | Capability |
|---|---|
| C1 | User-configurable lifecycle hooks that run external scripts/commands |
| C2a | Hook event fired **before** a tool executes |
| C2b | Hook event fired **after** a tool executes |
| C2c | Hook event at session start (incl. resume/clear) |
| C3 | Hook receives structured tool name + file path being touched |
| **C4** | **Hook output can inject context into the model's conversation (the core requirement)** |
| C5 | Hook can deny/block a tool call with a reason |
| C6 | User-facing message channel separate from model context |
| C7 | Hook registration can filter by tool name / event subtype |
| C8 | Session identifier available to the hook (dedup cache key) |
| C9 | Hooks fire inside subagents, with an agent identifier |
| C10 | Plugin/marketplace mechanism to distribute hooks + rule files to a team |
| C11 | Native conditional-rules feature that could substitute for part of the plugin |
| C12 | Project dir / plugin root exposed to the hook process |

**Rating legend:** ✅ yes (documented & verified) · 🟡 partial (exists with real gaps) · ❌ no (absent from docs / confirmed missing) · ❔ unknown (not documented either way)

---

## 2. Verdict summary

| Harness | Verdict | One-liner |
|---|---|---|
| **Claude Code** | ✅ Reference — shipping today | Fully functional. Also inherited for free by anything that *is* Claude Code with a different model backend (e.g. Z.ai's GLM Coding Plan). |
| **OpenAI Codex** | ✅ **Full port** | Hooks map ~1:1, including `additionalContext`, subagent `agent_id`, and plugin marketplace. Codex even sets `CLAUDE_PLUGIN_ROOT` aliases. Needs a path parser for `apply_patch`. |
| **Kimi CLI** | ✅ **Full port (solo installs only)** | Mechanically complete: exit-0 stdout is injected into model context. But hooks live only in the global `~/.kimi/config.toml` — no way to ship them to a team as a plugin. |
| **Z.ai ZCode** | 🟡 Partial — near-clone, unstable | Docs contract clones Claude Code (`additionalContext`, marketplace, compat env vars), but subagent hooks never fire, project-level hooks are disabled, and a P1 reliability bug is open. |
| **OpenCode** | 🟡 Partial — rewrite as TS plugin | No declarative hooks; the engine becomes a TypeScript plugin. Injection works but through different channels (tool-output mutation, system-prompt transform), not one clean field. |
| **Google Antigravity** | 🟡 Partial — two-hook bridge | Tool hooks see the file but can't inject; injection lives only at `PreInvocation`, which sees no file. A port bridges the two through a scratch file keyed by conversation id. |
| **Cursor** | 🟡 Partial — injection broken upstream | `additional_context` is documented for `postToolUse`/`sessionStart` but a Cursor engineer confirmed it doesn't reach the model (unfixed as of 2026-05). Native `.mdc` rules are the best glob fallback anywhere. |
| **xAI Grok Build** | ❌ Weakest — no injection channel | Rich hook plumbing, but no event lets hook output reach model context — stdout on passive events is explicitly ignored. Fallbacks: deny-with-reason, or Skills' file-touch `paths` globs. |

---

## Status timeline

The living layer of this report: what has moved since the snapshot, per harness, newest first. Every entry carries an evidence class:

- **docs** — verified against primary documentation; may change a matrix verdict.
- **reported** — credible community or research statement not yet reflected in official docs; never changes a verdict on its own. It graduates to **docs** when a citable primary source exists.
- **filed** — an issue we opened, with its current state.

### Antigravity

- 2026-08-18 · **reported** — hooks fire inside subagents, and every agent (parent and subagents alike) gets its own `conversationId`, per James O'Reilly (Google DevRel building on `invoke_subagent`/`manage_subagents`): [source](https://x.com/JamesOR/status/2089773238813372786), recorded on [#809](https://github.com/google-antigravity/antigravity-cli/issues/809#issuecomment-5332262566). The hooks docs do not state this yet, so the C9 verdict stays ❌ until a citable source exists (his article with working code is in progress). If confirmed, per-agent dedup keys on `conversationId` and the only remaining blocker is #808.
- 2026-08-18 · **docs** — C3 upgraded 🟡 → ✅: the current hooks documentation shows `PostToolUse` input includes `toolCall` (name and args), matching `PreToolUse`. Footnote 8 previously claimed the post side carried no tool or file info.
- 2026-08-17 · **filed** — [#808](https://github.com/google-antigravity/antigravity-cli/issues/808) (accept `injectSteps` on `PreToolUse`/`PostToolUse` responses — the remaining hard blocker) and [#809](https://github.com/google-antigravity/antigravity-cli/issues/809) (a `SessionStart` event, plus documentation and an identity field for subagent hook behavior). Both open.

### Pending re-verification (reported 2026-08-17, verdicts unchanged)

Research agents found primary-source evidence that these cells drifted after the snapshot. Each needs a fresh docs-class verification before its verdict is edited.

- **Codex** — hooks are GA and default-enabled since April 2026 ([openai/codex#19012](https://github.com/openai/codex/pull/19012)); footnote 1's opt-in flag claim is outdated.
- **Grok Build** — the in-repo user guide documents `additionalContext` working on `Stop`/`SubagentStop` hooks, so C4 ❌ overstates the gap (the ask narrows to extending the existing channel to `PreToolUse`/`PostToolUse`/`SessionStart`); and Skills' `paths` config adds skill-discovery directories rather than the file-touch trigger footnote 20 describes.
- **OpenCode** — an `agent` field was added to hook input ([anomalyco/opencode#13524](https://github.com/anomalyco/opencode/issues/13524), closed), and the `batch` tool was removed entirely; footnote 15 is outdated on both points.
- **Cursor** — `additional_context` on `preToolUse` is officially supported per a Cursor engineer ([forum](https://forum.cursor.com/t/166969)), with only a docs omission; the delivery bug for `sessionStart`/`postToolUse` remains live ([forum](https://forum.cursor.com/t/168441)). Footnote 12's "not preToolUse" claim is outdated; the blocked-upstream verdict stands.

---

## 3. Capability matrix

Ratings verified against primary docs on 2026-08-17. C4 is the capability the whole plugin is architected around. Columns are ordered by feasibility.

| Capability | Claude Code | Codex (OpenAI) | Kimi CLI (Moonshot) | ZCode (Z.ai) | OpenCode | Antigravity (Google) | Cursor | Grok Build (xAI) |
|---|---|---|---|---|---|---|---|---|
| C1 Command hooks (external scripts) | ✅ | ✅ ¹ | ✅ | ✅ ² | 🟡 ³ | ✅ | ✅ | ✅ |
| C2a Pre-tool event | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C2b Post-tool event | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C2c Session-start event | ✅ | ✅ | ✅ | ✅ | 🟡 ⁴ | 🟡 ⁵ | 🟡 ⁶ | ✅ |
| C3 Hook sees tool name + file path | ✅ | 🟡 ⁷ | ✅ | ✅ | ✅ | ✅ ⁸ | ✅ | 🟡 |
| **C4 Hook output injects model context** | **✅** | **✅** | **✅ ⁹** | **✅** | **✅ ¹⁰** | **✅ ¹¹** | **🟡 ¹²** | **❌** |
| C5 Block tool call with reason | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C6 User-facing message channel | ✅ | ✅ | 🟡 | 🟡 | ✅ | ❌ | ✅ | ❔ |
| C7 Tool / event matcher | ✅ | ✅ | ✅ | ✅ | 🟡 ¹³ | ✅ | ✅ | ✅ |
| C8 Session id (dedup cache key) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C9 Hooks fire in subagents (+ agent id) | ✅ | ✅ | 🟡 | ❌ ¹⁴ | 🟡 ¹⁵ | ❌ | 🟡 | 🟡 |
| C10 Plugin / marketplace distribution | ✅ | ✅ | ❌ ¹⁶ | ✅ ¹⁷ | ✅ | 🟡 | 🟡 | ✅ |
| C11 Native conditional rules | ✅ ¹⁸ | 🟡 | 🟡 | 🟡 | 🟡 | ✅ ¹⁹ | ✅ ¹⁹ | 🟡 ²⁰ |
| C12 Project / plugin paths for hook | ✅ | ✅ | 🟡 | ✅ | ✅ | 🟡 | ✅ | ✅ |
| **Verdict** | **shipping** | **full port** | **full port ¹⁶** | **partial** | **partial** | **partial** | **partial ¹²** | **weakest** |

### Footnotes

1. Hooks are gated behind an opt-in `features.hooks` flag in Codex config.
2. Project-level hook config is disabled in ZCode ("for security reasons") — plugin packaging is the only distribution path that works.
3. OpenCode has no declarative hook config; hooks are written as JS/TS plugin functions (`tool.execute.before/after`, etc.).
4. `session.created` exists, but whether it re-fires on resume or `/clear` is undocumented.
5. No SessionStart event; closest analog is `PreInvocation` with `invocationNum == 0`.
6. `sessionStart` fires on new conversation only; cloud agents are read-only for hooks.
7. File edits run through `apply_patch`, whose path is embedded in an unstructured `tool_input.command` string — needs a small parser instead of a field read.
8. Both `PreToolUse` and `PostToolUse` receive `toolCall` (name + args) on stdin; the post side adds `stepIdx` and an optional `error`. Corrected 2026-08-18 against the current hooks docs — the original snapshot claimed the post side carried only `stepIdx` + error.
9. Universal contract: any hook that exits 0 with non-empty stdout has that text added to model context.
10. No single field; verified channels are `tool.execute.after` output mutation, `experimental.chat.system.transform`, `experimental.chat.messages.transform`, and compaction context.
11. Injection (`injectSteps`) exists only at `PreInvocation`/`PostInvocation` — decoupled from tool events, which cannot inject.
12. Documented for `postToolUse`/`sessionStart` only (not `preToolUse`), and a Cursor engineer confirmed on the forum that delivery is broken as of April–May 2026, unfixed.
13. No per-registration matcher; the plugin function receives every event and filters in code (equivalent in practice).
14. Maintainer-confirmed 2026-08-17: subagent-scoped events "never fire at runtime" (open P1 feature request, zai-org/feedback#167).
15. Hooks do fire for subagent tool calls (an earlier bug report was a misdiagnosis), but payloads carry no agent-identity field, and the `batch` tool bypasses hooks entirely.
16. Hooks are configurable only in the single global `~/.kimi/config.toml`; plugin manifests carry tools only, not hooks — every teammate hand-edits config.
17. ZCode preloads the Claude Code marketplace and can install Claude Code plugins directly.
18. Nested `CLAUDE.md` is directory-scoped and always-on, but native path-specific rules (`.claude/rules/*.md` with `paths:` globs) are glob-triggered on file reads — the same class as Antigravity Rules and Cursor `.mdc` (¹⁹). Like those, they support no content-regex, project-shape checks, or combinators, and cannot ship inside plugins.
19. The two other genuinely conditional native systems (besides Claude Code's path-specific rules, ¹⁸): Antigravity Rules (manual / always-on / model-decision / glob) and Cursor `.mdc` (alwaysApply / globs / description). None of the three supports content-regex, project-shape checks, or AND/OR/NOT combinators.
20. Grok rules are always-on and directory-scoped, but Grok Skills support a `paths` field ("Gitignore globs. Hidden until a matching file is touched") — a native file-touch trigger.

---

## 4. Porting notes per harness

### 4.1 OpenAI Codex — **Full port**

Docs: learn.chatgpt.com (canonical; developers.openai.com/codex/hooks 308-redirects there).

- Near 1:1 contract: `PreToolUse`/`PostToolUse`/`SessionStart` (matcher on `startup|resume|clear|compact`), stdin JSON with `session_id`, `cwd`, `tool_name`, `tool_input`; output via `hookSpecificOutput.additionalContext` — verbatim doc quote: *"That additionalContext text is added as extra developer context."*
- `permissionDecision: deny` + reason for the config-error path; `systemMessage` as the separate UI channel; `SubagentStart/Stop` carry `agent_id`.
- Distribution via `.codex-plugin/plugin.json` manifests; one universal plugin directory shared by ChatGPT and Codex. Codex even sets `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PLUGIN_DATA` aliases for existing plugin hooks.
- **Adapter work:** file edits go through `apply_patch` — extract paths from the patch envelope (`*** Update File:` lines) instead of reading `tool_input.file_path`; matchers become `apply_patch`-style names; hooks must be enabled via `features.hooks` and trusted via `/hooks`.

Sources: https://learn.chatgpt.com/docs/hooks · https://learn.chatgpt.com/codex/agent-configuration/agents-md · https://learn.chatgpt.com/codex/build-plugins

### 4.2 Kimi CLI — **Full port (solo installs only)**

Docs: kimi-cli.com · 13 hook events, TOML config.

- Core mechanism is fully there: `PreToolUse`/`PostToolUse` receive `tool_name`/`tool_input` (file path included) + `session_id`; `SessionStart` covers startup/resume; regex `matcher` per hook entry.
- Injection is the simplest of any harness: exit 0 with non-empty stdout → *"stdout content (if non-empty) is added to context"* — universal across all 13 events.
- **The gap is distribution, not mechanics:** hooks are declared only as `[[hooks]]` entries in the global `~/.kimi/config.toml`. Plugin manifests carry tools only. Rule files can live in the repo, but every teammate must hand-add the hook entries.
- Subagent behavior unconfirmed: `SubagentStart/Stop` events exist, but nothing documents whether tool hooks fire inside a running subagent.

Sources: https://www.kimi-cli.com/en/customization/hooks.html · https://www.kimi-cli.com/en/customization/skills.html · https://www.kimi-cli.com/en/customization/plugins.html

### 4.3 Z.ai ZCode — **Partial: near-clone contract, reliability risk**

Docs: zcode.z.ai · plus the separate GLM Coding Plan story.

- **Two stories.** Z.ai's flagship motion is pointing Claude Code (or Cline/OpenCode/Cursor) at GLM endpoints — in that mode the harness *is* Claude Code and our plugin works unmodified today.
- ZCode itself clones the Claude Code contract: `hookSpecificOutput.additionalContext` confirmed with literal JSON examples across SessionStart/PreToolUse/PostToolUse/UserPromptSubmit/Stop; `permissionDecision`; pipe matchers; `session_id`; plugin marketplace that preloads the Claude Code marketplace.
- **Blockers today:** subagent hooks don't exist (maintainer-confirmed "never fire", open P1 request #167); project-level hook config is disabled, so plugin packaging is the only distribution; the same P1 thread reports hooks intermittently failing to fire, and the documented argv-form hook config can silently invalidate the whole config file. Smoke-test end-to-end before shipping; use shell-string commands.

Sources: https://zcode.z.ai/en/docs/hooks · https://zcode.z.ai/en/docs/plugin · https://github.com/zai-org/feedback/issues/167 · https://docs.z.ai/devpack/tool/claude

### 4.4 OpenCode — **Partial: rebuild as a TypeScript plugin**

Docs: opencode.ai · plugin API, no declarative hooks.

- The engine would be rewritten as a JS/TS plugin exporting `tool.execute.before/after` handlers — these expose tool name, `args.filePath`, and `sessionID`, so the whole condition tree evaluates fine.
- Injection works but has no single field: append matched rule text to `tool.execute.after`'s `output.output` (verified to reach the model; the two GitHub issues suggesting otherwise were a fixed bug and a misdiagnosis), or use `experimental.chat.system.transform` / `messages.transform` for turn-level injection.
- Subagent tool calls do fire hooks, but with no agent-identity field the per-agent dedup key degrades to session-only; the `batch` tool bypasses hooks entirely.
- Distribution is good: npm package in `opencode.json`'s `plugin` array (auto-installed) or committed `.opencode/plugins/` files.

Sources: https://opencode.ai/docs/plugins/ · https://opencode.ai/docs/rules/ · https://github.com/anomalyco/opencode/issues/5894 · https://github.com/anomalyco/opencode/issues/3384

### 4.5 Google Antigravity — **Partial: needs a two-hook bridge**

Docs: antigravity.google/docs/ide · IDE hook system (CLI variant undocumented, its docs page 404s).

- The two halves of our design live on different events: `PreToolUse` sees `toolCall` (name + args) and can allow/deny with reason but has no injection field; `injectSteps` (ephemeralMessage/userMessage into the trajectory) exists only on `PreInvocation`/`PostInvocation`, which carry no file info.
- **Bridge design:** PreToolUse evaluates rules against `toolCall.args` and stages matched text in a scratch file keyed by `conversationId`; the next PreInvocation reads, injects, and clears it. Dedup survives via conversationId.
- No SessionStart event, no subagent hooks, no plugin-root env var, and third-party plugins install by manual folder placement only (the in-UI browser covers Google-bundled plugins only).
- Native Rules (manual / always-on / model-decision / glob, 12k chars each) cover the pure-glob subset hook-free.

Sources: https://antigravity.google/docs/ide/hooks/ · https://antigravity.google/docs/ide/rules/ · https://antigravity.google/docs/ide/plugins/

### 4.6 Cursor — **Partial: blocked on an upstream bug**

Docs: cursor.com/docs · hooks + the strongest native rules system.

- Plumbing is solid: `hooks.json` at four levels (Enterprise > Team > Project > User), `beforeReadFile`/`afterFileEdit` expose a literal `file_path`, block-with-reason, conversation id, and even LLM-evaluated "prompt-based" hooks.
- **The blocker:** `additional_context` exists only on `postToolUse`/`sessionStart` (not `preToolUse`), and a Cursor engineer confirmed on the forum it is not actually delivered to the model (April–May 2026, no fix on record). Re-check the changelog before committing — if still broken, Cursor is effectively native-substitute-only.
- Native `.cursor/rules/*.mdc` is the best fallback of any harness: `alwaysApply`, `globs` auto-attach, and description-based agent choice — covering our always-on and path-glob rules, though never content-regex, project-shape, or combinators.
- Subagent tool events carry no distinguishing id; team distribution is git-committed `.cursor/` or the Enterprise dashboard (plus a new "Customize" plugin-install surface for hooks).

Sources: https://cursor.com/docs/hooks · https://cursor.com/docs/rules · https://forum.cursor.com/t/158452

### 4.7 xAI Grok Build — **Weakest fit: no injection channel**

Docs: docs.x.ai/build · real product (github.com/xai-org/grok-build), rich hooks, one fatal gap.

- 14 events, tool-name matchers, `sessionId`, JSON stdin, plugins + marketplaces — and it even re-reads Claude Code's `.claude/settings.json` and Cursor's `hooks.json` hook formats.
- **But no event can inject context:** PreToolUse's only output is `{"decision":"deny","reason":...}`; for passive events "stdout is ignored". Nothing resembling `additionalContext` exists anywhere in the schema.
- Degraded options: (a) deny-with-reason then let the model retry — disruptive and semantically different; (b) native Skills with the `paths` field — gitignore-style globs that reveal a skill when a matching file is touched, the closest native analog to a path-triggered rule; (c) always-on `.grok/rules/*.md` / AGENTS.md for unconditional content.

Sources: https://docs.x.ai/build/features/hooks · https://docs.x.ai/build/features/project-rules · https://docs.x.ai/build/features/skills-plugins-marketplaces

---

## 5. Native rules systems compared (C11 detail)

| Harness | Native feature | Trigger conditions | Covers our engine? |
|---|---|---|---|
| Claude Code | Nested `CLAUDE.md` / path-specific rules (`.claude/rules/*.md` + `paths:` globs) / skills | Always-on (`CLAUDE.md`); **glob on read file** (path rules); skills = agent-decides | Always-on + path-glob subsets — content-regex, shape checks, combinators, and plugin distribution still need this plugin |
| Codex | `AGENTS.md` chain (global → git root → cwd, deeper overrides) | Directory placement only; explicitly no globs, no frontmatter, no conditions | Always-on subset only |
| Kimi CLI | `AGENTS.md` via `${KIMI_AGENTS_MD}` + Skills | Always-on merge; skills = agent-decides (free-text description) | Always-on subset only |
| ZCode | Skills (name + ≤250-char description injected every turn) | Agent-decides only | Coarse agent-decides only |
| OpenCode | `AGENTS.md` + `opencode.json` `instructions` (paths/globs/URLs) | Directory scope; glob selects which files to load, all loaded unconditionally | Always-on subset only |
| Antigravity | Rules (`.agents/rules`, ≤12k chars each) | One mode per rule: manual / always-on / model-decision / **glob on touched file** | Always-on + path-glob subsets |
| Cursor | `.cursor/rules/*.mdc` + nested `AGENTS.md` | `alwaysApply` / **`globs` auto-attach** / description (agent-decides) / manual @-mention | Always-on + path-glob + agent-decides subsets (best native) |
| Grok Build | `.grok/rules/*.md` + AGENTS/CLAUDE.md loader; Skills `paths` | Rules: directory-scoped always-on, no conditions; Skills: **gitignore-glob file-touch reveal** | Always-on subset + glob-ish skill reveal |

No harness natively supports content-regex conditions, arbitrary-project-file existence/content checks, or `all_of`/`any_of`/`not` combinators. That condition tree only exists where a hook can run our evaluator.

---

## 6. Feasibility ranking

1. **Codex port first** — highest fidelity, smallest delta. Main work: `apply_patch` path parser, matcher renames, and documenting the `features.hooks` opt-in for consumers.
2. **Kimi CLI second, as a "bring your own config" port** — the engine drops in nearly unchanged (stdout injection is even simpler than Claude Code's JSON), with a documented manual `[[hooks]]` setup step per user.
3. **ZCode: wait or smoke-test heavily** — the contract is a clone, but the open P1 reliability issue, missing subagent events, and disabled project hooks mean any port must be verified end-to-end, not assumed from docs. Meanwhile, GLM-through-Claude-Code users already get the plugin as-is.
4. **OpenCode if there's demand** — a real but separate codebase (TypeScript plugin), publishable to npm.
5. **Cursor: watch the `additional_context` bug** — ship `.mdc` glob rules as a stopgap; revisit hooks when injection is fixed.
6. **Antigravity only with the two-hook bridge design**, accepting no subagent coverage and manual installs.
7. **Grok Build: don't port** — offer the Skills-`paths` + always-on rules degraded profile instead.

---

## 7. Methodology

14 research and verification agents (model: Sonnet) ran on 2026-08-17: one researcher per harness followed by an independent adversarial fact-checker that re-fetched primary docs and hunted for fabricated quotes. The verification pass caught and corrected 21 errors before anything was accepted into this report — including two fabricated doc quotes in the xAI assessment (verdicts held on real evidence), a wrong "subagents bypass hooks" verdict for OpenCode, and a full-port → partial-port downgrade for ZCode after finding a maintainer-acknowledged P1 reliability issue. All seven final verdicts carry high verification confidence.

Notable source-integrity checks performed by the verifiers:

- `learn.chatgpt.com` confirmed as OpenAI's real canonical Codex docs host (developers.openai.com/codex/hooks 308-redirects to it), not a typosquat.
- "Grok Build" confirmed as a real shipping product via github.com/xai-org/grok-build and x.ai/news, not a hallucination.
- A transient CDN anomaly served ZCode docs under a kimi-cli.com URL during one fetch; the Kimi findings were re-verified byte-for-byte across repeated fetches of the `.md` companion endpoints and cross-checked against github.com/MoonshotAI/kimi-cli.
- One WebFetch summarizer hallucination was caught during Z.ai verification (it described GLM as "Anthropic's large language model"); load-bearing Z.ai claims were re-verified against raw HTML instead.
