#!/usr/bin/env python3
"""
Shared core for the conditional-rules hook system (plugin edition).

Three entry-point scripts import `run_entry()` from this module:
  - conditional_rules_pre.py      (PreToolUse)
  - conditional_rules_post.py     (PostToolUse)
  - conditional_rules_session.py  (SessionStart)

Rule sources are discovered at runtime from two locations:

  1. Marketplace plugins — read from the Claude Code installed-plugins
     registry at `$CLAUDE_INSTALLED_PLUGINS_FILE` (defaults to
     `$HOME/.claude/plugins/installed_plugins.json`). Each scope record's
     `installPath` is the version-pinned cache directory the plugin actually
     runs from; we look for `rules.json` directly under that path. Stale
     older versions still on disk under the cache root are ignored.

     Each record is gated by its own `scope`: `user` and `managed` records
     apply everywhere, while `project` and `local` records only apply when
     the record's `projectPath` is an absolute path resolving to the project
     the hook is running in (a relative `projectPath` is rejected — it would
     resolve against the hook's cwd and match every project). Records with a
     missing, non-string, or unrecognized `scope` are skipped and reported on
     stderr.

     If the registry file is absent, we fall back to a recursive scan of
     `$CLAUDE_MARKETPLACE_DIR` (defaults to `$HOME/.claude/plugins/marketplaces`)
     so authoring/dev workflows that pre-date the registry still work. Set
     `CLAUDE_INSTALLED_PLUGINS_FILE` to a non-existent path to force the
     fallback.

     If `rules.json` defines `activation_criteria`, the condition is evaluated
     against the current project; the source is skipped when it evaluates to
     False. Content files are resolved relative to `installPath` (or, in
     fallback mode, the folder that contains `rules.json`).

  2. Project rules — `$CLAUDE_PROJECT_DIR/.claude/conditional_rules/rules.json`.
     Loaded unconditionally (no `activation_criteria` check). Content files are
     resolved relative to `$CLAUDE_PROJECT_DIR/.claude/conditional_rules/`.

The hook is session-scoped: per-session dedup state lives under
`${CLAUDE_PLUGIN_ROOT}/hooks/conditional_rules/.state/`.

See CONDITIONAL_RULES.md for the authoring-facing guide.
"""

from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

MAX_CONTENT_READ_BYTES = 1 * 1024 * 1024
STATE_SWEEP_AGE_SECONDS = 7 * 24 * 3600

SUPPORTED_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")

TRIGGERING_FILE_PREDICATES = (
    "triggering_file_path_glob",
    "triggering_file_path_regex",
    "triggering_file_content_regex",
)
CONTEXT_ONLY_PREDICATES = (
    "any_file_exists",
    "any_file_content_regex",
)
PREDICATE_KEYS = TRIGGERING_FILE_PREDICATES + CONTEXT_ONLY_PREDICATES
COMBINATOR_KEYS = ("all_of", "any_of", "not")
TOOL_ALLOWLIST = ("Edit", "Write", "Read")

KNOWN_TOP_LEVEL = frozenset({"$schema", "activation_criteria", "rules"})
KNOWN_RULE_FIELDS = frozenset(
    {
        "id",
        "description",
        "enabled",
        "when",
        "content",
        "content_file",
        "tools",
        "fires_on_matcher",
    }
)


class ConfigError(Exception):
    """Raised when rules.json is structurally or semantically invalid."""


@dataclass
class Rule:
    id: str
    description: str | None
    enabled: bool
    when: dict
    content: str | None
    content_file: Path | None
    tools: tuple[str, ...] | None
    fires_on_matcher: str
    reached_events: frozenset[str]
    uses_triggering_file_predicates: bool
    uses_any_file_predicates: bool

    def resolve_content(self) -> str | None:
        """Return the rule's content, reading `content_file` lazily.

        A missing or unreadable `content_file` is a runtime error (not a config
        error): log to stderr and return None. Callers skip the rule without
        blocking the tool.
        """
        if self.content is not None:
            return self.content
        if self.content_file is not None:
            try:
                return self.content_file.read_text(encoding="utf-8")
            except OSError as exc:
                print(
                    f"conditional_rules: failed to read content_file {self.content_file}: {exc}",
                    file=sys.stderr,
                )
                return None
        return None


@dataclass
class LoadResult:
    """Result of loading and validating a rules.json file."""

    rules: list[Rule]
    activation_criteria: dict | None


@dataclass
class RuleSource:
    """A single rules.json discovered in the marketplace or project directory.

    `content_root` is the base used for resolving `content_file` paths; it is
    always the directory that contains `rules.json`.

    `check_activation` is True for marketplace sources (where
    `activation_criteria` governs whether the source applies to the current
    project) and False for the project-level source (always applied).
    """

    rules_file: Path
    content_root: Path
    check_activation: bool


@dataclass
class Paths:
    """Filesystem locations the hook reads and writes.

    - `installed_plugins_file` is Claude Code's plugin registry. When it
      exists, each scope record's `installPath` is treated as a plugin root
      to look for `rules.json` in, provided the record's `scope` puts it in
      force for `project_root`. This is the preferred discovery source.
    - `marketplace_dir` is scanned recursively for `rules.json` files only
      when `installed_plugins_file` is absent (legacy fallback).
    - `user_settings_file` is `~/.claude/settings.json`. Its `enabledPlugins`
      section, merged with project-scope settings, is what gates whether an
      installed plugin contributes its rules — `/plugin disable` flips a
      key to `false` there. The registry never carries this state.
    - `project_root` is the user's repo — used for triggering-file path
      normalization, `any_file_*` predicates, and matching the `projectPath`
      of project/local-scope registry records.
    - `state_dir` is where per-session dedup cache files are stored; it lives
      under the conditional-rules plugin root.
    """

    marketplace_dir: Path
    installed_plugins_file: Path
    user_settings_file: Path
    project_root: Path
    state_dir: Path

    @classmethod
    def from_env(cls, hook_input: dict | None = None) -> Paths:
        plugin_root = _detect_plugin_root()
        project_root = _detect_project_root(hook_input or {})
        return cls(
            marketplace_dir=_detect_marketplace_dir(),
            installed_plugins_file=_detect_installed_plugins_file(),
            user_settings_file=_detect_user_settings_file(),
            project_root=project_root,
            state_dir=plugin_root / "hooks" / "conditional_rules" / ".state",
        )

    @classmethod
    def for_test(
        cls,
        plugin_root: Path,
        project_root: Path,
        marketplace_dir: Path | None = None,
        installed_plugins_file: Path | None = None,
        user_settings_file: Path | None = None,
    ) -> Paths:
        """Explicit constructor for unit tests.

        `plugin_root` is used only to derive `state_dir`. `marketplace_dir`,
        `installed_plugins_file`, and `user_settings_file` default to
        nonexistent paths so tests that do not opt in to marketplace
        discovery or `enabledPlugins` filtering see no sources, and so no
        test ever leaks reads from the user's real registry or settings.
        """
        mdir = marketplace_dir if marketplace_dir is not None else plugin_root / "_no_marketplace_"
        ipf = (
            installed_plugins_file
            if installed_plugins_file is not None
            else plugin_root / "_no_registry_.json"
        )
        usf = (
            user_settings_file
            if user_settings_file is not None
            else plugin_root / "_no_user_settings_.json"
        )
        return cls(
            marketplace_dir=mdir,
            installed_plugins_file=ipf,
            user_settings_file=usf,
            project_root=project_root,
            state_dir=plugin_root / "hooks" / "conditional_rules" / ".state",
        )


@dataclass
class EvalContext:
    """Per-invocation inputs passed to the evaluator."""

    file_path_abs: Path | None
    file_path_rel: str | None
    project_root: Path
    _content_cache: dict[str, str | None] = field(default_factory=dict)

    def read_content(self) -> str | None:
        """Read the triggering file's content (cached per-invocation)."""
        if self.file_path_abs is None:
            return None
        return self._read_with_cache(self.file_path_abs)

    def read_any_file(self, abs_path: Path) -> str | None:
        """Read an arbitrary file's content (cached per-invocation)."""
        return self._read_with_cache(abs_path)

    def _read_with_cache(self, abs_path: Path) -> str | None:
        key = str(abs_path)
        if key in self._content_cache:
            return self._content_cache[key]
        text = _read_file_capped(abs_path, MAX_CONTENT_READ_BYTES)
        self._content_cache[key] = text
        return text


# ---------- Config loading & validation ----------


def load_rules(rules_file: Path, content_root: Path) -> LoadResult | None:
    """Parse and validate rules.json. Returns None if the file is absent.

    `content_root` is the directory used to resolve `content_file` paths and
    to enforce that they stay within the source's own directory tree. It must
    be the directory that contains `rules_file`.
    """
    if not rules_file.is_file():
        return None
    try:
        raw = rules_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"rules.json unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"rules.json not valid JSON: {exc.msg} (line {exc.lineno} col {exc.colno})"
        ) from exc
    rules, activation_criteria = _validate_config(data, content_root)
    return LoadResult(rules=rules, activation_criteria=activation_criteria)


def _validate_config(data: Any, content_root: Path) -> tuple[list[Rule], dict | None]:
    if not isinstance(data, dict):
        raise ConfigError("rules.json root must be an object with a 'rules' key")
    unknown = set(data.keys()) - KNOWN_TOP_LEVEL
    if unknown:
        raise ConfigError(
            f"rules.json has unknown top-level key(s): {sorted(unknown)}; allowed: {sorted(KNOWN_TOP_LEVEL)}"
        )
    rules_raw = data.get("rules")
    if not isinstance(rules_raw, list):
        raise ConfigError("rules.json 'rules' field must be a list")

    activation_criteria: dict | None = None
    if "activation_criteria" in data:
        ac = data["activation_criteria"]
        if not isinstance(ac, dict):
            raise ConfigError("rules.json 'activation_criteria' must be a condition object")
        predicates_used: set[str] = set()
        _validate_condition(ac, "activation_criteria", predicates_used)
        bad = predicates_used & set(TRIGGERING_FILE_PREDICATES)
        if bad:
            raise ConfigError(
                f"activation_criteria uses triggering_file_* predicate(s) {sorted(bad)}; "
                f"activation_criteria is evaluated against the project (not a specific tool "
                f"invocation) and must use only any_file_* predicates"
            )
        activation_criteria = ac

    seen_ids: set[str] = set()
    out: list[Rule] = []
    for idx, rule in enumerate(rules_raw):
        out.append(_validate_rule(rule, idx, content_root, seen_ids))
    return out, activation_criteria


def _format_loc(idx: int, description: str | None) -> str:
    if description:
        return f"rules[{idx}] ({description!r})"
    return f"rules[{idx}]"


def _validate_rule(
    rule: Any,
    idx: int,
    content_root: Path,
    seen_ids: set[str],
) -> Rule:
    if not isinstance(rule, dict):
        raise ConfigError(f"rules[{idx}] must be an object")

    unknown = set(rule.keys()) - KNOWN_RULE_FIELDS
    if unknown:
        raise ConfigError(
            f"rules[{idx}] has unknown field(s): {sorted(unknown)}; allowed: {sorted(KNOWN_RULE_FIELDS)}"
        )

    description: str | None = None
    if "description" in rule:
        desc = rule["description"]
        if not isinstance(desc, str) or not desc:
            raise ConfigError(f"rules[{idx}].description must be a non-empty string")
        description = desc

    loc = _format_loc(idx, description)

    rid = rule.get("id")
    if not isinstance(rid, str) or not rid:
        raise ConfigError(f"{loc}.id must be a non-empty string")
    if rid in seen_ids:
        raise ConfigError(f"{loc}.id {rid!r} duplicated elsewhere in rules.json")
    seen_ids.add(rid)

    enabled = True
    if "enabled" in rule:
        val = rule["enabled"]
        if not isinstance(val, bool):
            raise ConfigError(f"{loc}.enabled must be a JSON boolean (got {type(val).__name__})")
        enabled = val

    when = rule.get("when")
    if not isinstance(when, dict):
        raise ConfigError(f"{loc}.when must be an object with exactly one key")
    predicates_used: set[str] = set()
    _validate_condition(when, f"{loc}.when", predicates_used)

    tools: tuple[str, ...] | None = None
    if "tools" in rule:
        tools = _validate_tools(rule["tools"], loc)

    matcher_raw = rule.get("fires_on_matcher", "PreToolUse")
    if not isinstance(matcher_raw, str):
        raise ConfigError(f"{loc}.fires_on_matcher must be a string")
    reached = _compute_reached_events(matcher_raw, loc)
    if not reached:
        raise ConfigError(
            f"{loc}: fires_on_matcher {matcher_raw!r} does not match any supported event "
            f"({', '.join(SUPPORTED_EVENTS)})"
        )

    tool_events_reached = reached & set(TOOL_EVENTS)
    non_tool_events_reached = reached - set(TOOL_EVENTS)
    triggering_used = bool(predicates_used & set(TRIGGERING_FILE_PREDICATES))

    if triggering_used and non_tool_events_reached:
        offending = min(non_tool_events_reached)
        raise ConfigError(
            f"{loc}: uses triggering_file_* predicate but fires_on_matcher {matcher_raw!r} "
            f"can fire on {offending} (which has no triggering file)"
        )

    if tools is not None and not tool_events_reached:
        raise ConfigError(
            f"{loc}: tools is set but fires_on_matcher {matcher_raw!r} does not fire on any "
            f"tool-bearing event"
        )

    has_content = "content" in rule
    has_content_file = "content_file" in rule
    if has_content == has_content_file:
        raise ConfigError(
            f"{loc} must have exactly one of 'content' or 'content_file' (got both or neither)"
        )

    if has_content:
        content = _normalize_content(rule["content"], loc)
        content_file: Path | None = None
    else:
        content = None
        content_file = _validate_content_file_spec(rule["content_file"], content_root, loc)

    return Rule(
        id=rid,
        description=description,
        enabled=enabled,
        when=when,
        content=content,
        content_file=content_file,
        tools=tools,
        fires_on_matcher=matcher_raw,
        reached_events=frozenset(reached),
        uses_triggering_file_predicates=triggering_used,
        uses_any_file_predicates=bool(predicates_used & set(CONTEXT_ONLY_PREDICATES)),
    )


def _validate_tools(value: Any, loc: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{loc}.tools must be a list")
    if not value:
        raise ConfigError(f"{loc}.tools must be a non-empty list")
    for i, entry in enumerate(value):
        if not isinstance(entry, str):
            raise ConfigError(f"{loc}.tools[{i}] must be a string")
        if entry not in TOOL_ALLOWLIST:
            raise ConfigError(
                f"{loc}: tools entry {entry!r} is not a valid tool name "
                f"(allowed: {', '.join(TOOL_ALLOWLIST)})"
            )
    return tuple(value)


def _compute_reached_events(matcher: str, loc: str) -> set[str]:
    if matcher in ("*", ""):
        return set(SUPPORTED_EVENTS)
    if re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
        pieces = matcher.split("|")
        return {e for e in SUPPORTED_EVENTS if e in pieces}
    try:
        pat = re.compile(matcher)
    except re.error as exc:
        raise ConfigError(f"{loc}: fires_on_matcher is not a valid regex: {exc}") from exc
    return {e for e in SUPPORTED_EVENTS if pat.search(e) is not None}


def matches_event(matcher: str | None, event_name: str) -> bool:
    """Evaluate a Claude-Code-style matcher against an event name."""
    if matcher is None or matcher in ("*", ""):
        return True
    if re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
        return event_name in matcher.split("|")
    return re.search(matcher, event_name) is not None


def _validate_condition(
    node: Any,
    loc: str,
    predicates_used: set[str],
) -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"{loc} must be an object")
    if len(node) != 1:
        raise ConfigError(
            f"{loc} must have exactly one key (one of {list(PREDICATE_KEYS) + list(COMBINATOR_KEYS)}), "
            f"got {list(node)}"
        )
    ((key, value),) = node.items()

    if key in PREDICATE_KEYS:
        _validate_predicate(key, value, loc)
        predicates_used.add(key)
        return

    if key == "all_of" or key == "any_of":
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{loc}.{key} must be a non-empty list")
        for i, child in enumerate(value):
            _validate_condition(child, f"{loc}.{key}[{i}]", predicates_used)
        return

    if key == "not":
        _validate_condition(value, f"{loc}.not", predicates_used)
        return

    raise ConfigError(
        f"{loc} has unknown key {key!r}; allowed: {list(PREDICATE_KEYS) + list(COMBINATOR_KEYS)}"
    )


def _validate_predicate(key: str, value: Any, loc: str) -> None:
    if key == "any_file_content_regex":
        _validate_any_file_content_regex(value, loc)
        return

    if not isinstance(value, str) or not value:
        raise ConfigError(f"{loc}.{key} must be a non-empty string")
    if key == "any_file_exists":
        if not _is_safe_rel_path(value):
            raise ConfigError(f"{loc}.{key} {value!r} must be a relative path inside project root")
        return
    if key in ("triggering_file_path_regex", "triggering_file_content_regex"):
        try:
            re.compile(value)
        except re.error as exc:
            raise ConfigError(f"{loc}.{key} is not a valid regex: {exc}") from exc


def _validate_any_file_content_regex(value: Any, loc: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(
            f"{loc}.any_file_content_regex must be an object with 'path' and 'pattern'"
        )
    unknown = set(value.keys()) - {"path", "pattern"}
    if unknown:
        raise ConfigError(
            f"{loc}.any_file_content_regex has unknown key(s): {sorted(unknown)}; "
            f"allowed: ['path', 'pattern']"
        )
    path_val = value.get("path")
    pattern_val = value.get("pattern")
    if not isinstance(path_val, str) or not path_val:
        raise ConfigError(f"{loc}.any_file_content_regex.path must be a non-empty string")
    if not isinstance(pattern_val, str) or not pattern_val:
        raise ConfigError(f"{loc}.any_file_content_regex.pattern must be a non-empty string")

    if not _is_safe_rel_path(path_val):
        raise ConfigError(
            f"{loc}.any_file_content_regex.path {path_val!r} must be a relative path inside project root"
        )

    try:
        re.compile(pattern_val)
    except re.error as exc:
        raise ConfigError(
            f"{loc}.any_file_content_regex.pattern is not a valid regex: {exc}"
        ) from exc


def _is_safe_rel_path(rel_path: str) -> bool:
    """True if `rel_path` is a relative POSIX path with no '..' segments.

    `any_file_*` predicates resolve against the user's project root at runtime,
    which is not known at config-load time. This pure-syntactic check prevents
    obvious escape attempts (`..`, absolute paths) without requiring a root.
    """
    p = PurePosixPath(rel_path)
    if p.is_absolute():
        return False
    return ".." not in p.parts


def _normalize_content(content: Any, loc: str) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(s, str) for s in content):
        return "\n".join(content)
    raise ConfigError(f"{loc}.content must be a string or list of strings")


def _validate_content_file_spec(rel_path: Any, content_root: Path, loc: str) -> Path:
    """Validate the shape and location of a `content_file` reference.

    Paths are resolved relative to `content_root` (the directory containing
    `rules.json`) and must stay inside it. Existence and readability are checked
    at match time (see Rule.resolve_content) so that a reference to a not-yet-
    created file does not block tool calls and does not cause a bootstrapping
    deadlock when authoring rules.
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise ConfigError(f"{loc}.content_file must be a non-empty string")
    candidate = (content_root / rel_path).resolve()
    content_root_resolved = content_root.resolve()
    try:
        candidate.relative_to(content_root_resolved)
    except ValueError as exc:
        raise ConfigError(f"{loc}.content_file {rel_path!r} escapes plugin root") from exc
    return candidate


# ---------- Evaluation ----------


def evaluate(node: dict, ctx: EvalContext) -> bool:
    """Evaluate a validated condition tree. Short-circuits on AND/OR."""
    ((key, value),) = node.items()

    if key == "all_of":
        for child in value:
            if not evaluate(child, ctx):
                return False
        return True
    if key == "any_of":
        for child in value:
            if evaluate(child, ctx):
                return True
        return False
    if key == "not":
        return not evaluate(value, ctx)
    if key == "triggering_file_path_glob":
        return _match_path_glob(ctx.file_path_rel, value)
    if key == "triggering_file_path_regex":
        if ctx.file_path_rel is None:
            return False
        return re.search(value, ctx.file_path_rel) is not None
    if key == "triggering_file_content_regex":
        if ctx.file_path_rel is None:
            return False
        text = ctx.read_content()
        if text is None:
            return False
        return re.search(value, text) is not None
    if key == "any_file_exists":
        return _any_file_exists(ctx.project_root, value)
    if key == "any_file_content_regex":
        abs_path = (ctx.project_root / value["path"]).resolve()
        text = ctx.read_any_file(abs_path)
        if text is None:
            return False
        return re.search(value["pattern"], text) is not None
    return False


def _any_file_exists(project_root: Path, pattern: str) -> bool:
    """Check if any file matching `pattern` exists under `project_root`.

    Supports both exact relative paths and glob patterns (including `**`).
    "." is the always-true sentinel documented in the spec (project root itself).
    """
    if pattern == ".":
        return project_root.is_dir()
    try:
        return any(True for _ in project_root.glob(pattern))
    except (OSError, ValueError):
        return False


def _match_path_glob(rel_path: str | None, pattern: str) -> bool:
    if rel_path is None:
        return False
    try:
        if PurePath(rel_path).full_match(pattern):
            return True
    except (AttributeError, ValueError):
        pass
    return fnmatch.fnmatchcase(rel_path, pattern)


def _read_file_capped(path: Path, max_bytes: int) -> str | None:
    try:
        with path.open("rb") as fh:
            data = fh.read(max_bytes)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


# ---------- Path normalization ----------


def normalize_project_relative(file_path_abs: str | None, project_root: Path) -> str | None:
    """Return a POSIX-style project-relative path, or None if outside the project.

    Also returns None when either path cannot be resolved at all — an `OSError`
    (loops, permissions) or a `ValueError` (an embedded NUL byte, which
    `Path.resolve()` raises rather than `OSError`). A caller that gets None
    simply evaluates rules with no triggering file; letting the exception
    escape would kill rule injection for the whole event.
    """
    if not file_path_abs:
        return None
    try:
        abs_real = Path(file_path_abs).resolve()
        root_real = project_root.resolve()
    except (OSError, ValueError):
        return None
    try:
        rel = abs_real.relative_to(root_real)
    except ValueError:
        return None
    return rel.as_posix()


# ---------- Session cache ----------


def load_cache(cache_file: Path) -> dict[str, dict]:
    """Read the session cache as an ordered mapping of rule_id -> audit entry.

    Returns an empty dict on any error. Insertion order is preserved (Python
    3.7+ dict semantics) so the file remains an audit log of activations.
    """
    if not cache_file.is_file():
        return {}
    try:
        with cache_file.open("r", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                data = json.load(fh)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError):
        return {}
    injected = data.get("injected") if isinstance(data, dict) else None
    if not isinstance(injected, dict):
        return {}
    return {
        rid: meta
        for rid, meta in injected.items()
        if isinstance(rid, str) and isinstance(meta, dict)
    }


def append_to_cache(cache_file: Path, new_entries: dict[str, dict]) -> None:
    """Merge new entries into the cache atomically under an exclusive flock.

    Existing entries keep their original audit metadata and ordering; new
    entries are appended in iteration order after the existing ones.
    """
    if not new_entries:
        return
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(cache_file), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with os.fdopen(fd, "r+", encoding="utf-8", closefd=False) as fh:
                fh.seek(0)
                raw = fh.read()
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    data = {}
                existing: dict[str, dict] = {}
                if isinstance(data, dict) and isinstance(data.get("injected"), dict):
                    existing = {
                        rid: meta
                        for rid, meta in data["injected"].items()
                        if isinstance(rid, str) and isinstance(meta, dict)
                    }
                merged: dict[str, dict] = dict(existing)
                for rid, meta in new_entries.items():
                    if rid not in merged:
                        merged[rid] = meta
                fh.seek(0)
                fh.truncate()
                json.dump({"injected": merged}, fh, indent=2)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def wipe_cache(cache_file: Path) -> None:
    """Remove a session cache file. Missing file is a no-op."""
    try:
        cache_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(
            f"conditional_rules: failed to remove {cache_file}: {exc}",
            file=sys.stderr,
        )


def ensure_project_state_symlink(cache_file: Path, project_root: Path) -> None:
    """Mirror the canonical session cache file as a symlink in the project tree.

    When the project has its own `.claude/conditional_rules/rules.json`,
    create a symlink at `<project>/.claude/conditional_rules/.state/<session>.json`
    pointing at the canonical cache file (which lives in the plugin cache,
    e.g. `~/.claude/plugins/cache/<mp>/<plug>/<ver>/hooks/conditional_rules/.state/`).

    This exists purely for troubleshooting: users can `cat` the audit log
    from inside their repo without navigating into the plugin cache.

    The function is a no-op when:
    - The project does not have project-level rules (no symlink target makes
      sense if the user isn't authoring project rules).
    - The state directory cannot be created.
    - A non-symlink already exists at the destination (we never clobber
      user data; we log the conflict to stderr instead).
    - `os.symlink` fails — e.g. on Windows without the right privilege. The
      hook keeps working; users just lose the convenience link.
    """
    project_rules_file = project_root / ".claude" / "conditional_rules" / "rules.json"
    if not project_rules_file.is_file():
        return

    state_link_dir = project_root / ".claude" / "conditional_rules" / ".state"
    link_path = state_link_dir / cache_file.name

    try:
        state_link_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"conditional_rules: failed to create project state dir {state_link_dir}: {exc}",
            file=sys.stderr,
        )
        return

    if link_path.is_symlink():
        try:
            current_target = os.readlink(link_path)
        except OSError:
            current_target = None
        if current_target == str(cache_file):
            return  # idempotent: already pointing at the right place
        try:
            link_path.unlink()
        except OSError as exc:
            print(
                f"conditional_rules: failed to replace stale symlink {link_path}: {exc}",
                file=sys.stderr,
            )
            return
    elif link_path.exists():
        # A regular file or directory already exists here — never clobber.
        print(
            f"conditional_rules: refusing to replace non-symlink at {link_path} "
            f"(remove it manually if you want the troubleshooting link)",
            file=sys.stderr,
        )
        return

    try:
        link_path.symlink_to(cache_file)
    except OSError as exc:
        print(
            f"conditional_rules: failed to create state symlink at {link_path}: {exc}",
            file=sys.stderr,
        )


def opportunistic_sweep(state_dir: Path, now: float, max_age: int) -> None:
    if not state_dir.is_dir():
        return
    for entry in state_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if now - mtime > max_age:
            try:
                entry.unlink()
            except OSError:
                pass


# ---------- Source discovery ----------


def _collect_rule_sources(
    project_root: Path,
    marketplace_dir: Path,
    installed_plugins_file: Path,
    user_settings_file: Path,
) -> list[RuleSource]:
    """Return all active rule sources for the current invocation.

    Sources are collected in two passes:
      1. Marketplace plugins. Preferred: read each plugin's pinned
         `installPath` from `installed_plugins_file` and look for
         `rules.json` directly there. Records installed at project or
         local scope for a *different* project than `project_root` are
         skipped (see `_discover_from_registry`). Plugins flipped to
         `false` in any `enabledPlugins` section reachable from
         `user_settings_file` or `project_root` are skipped — they're
         installed but disabled. If the registry is absent (or set to a
         non-existent path via env), fall back to a recursive scan of
         `marketplace_dir`.
      2. `$CLAUDE_PROJECT_DIR/.claude/conditional_rules/rules.json` (if present).
    """
    sources: list[RuleSource] = []

    if installed_plugins_file.is_file():
        disabled = _load_disabled_plugins(user_settings_file, project_root)
        sources.extend(_discover_from_registry(installed_plugins_file, project_root, disabled))
    elif marketplace_dir.is_dir():
        for rules_file in sorted(marketplace_dir.rglob("rules.json")):
            sources.append(
                RuleSource(
                    rules_file=rules_file,
                    content_root=rules_file.parent,
                    check_activation=True,
                )
            )

    project_rules_file = project_root / ".claude" / "conditional_rules" / "rules.json"
    if project_rules_file.is_file():
        sources.append(
            RuleSource(
                rules_file=project_rules_file,
                content_root=project_rules_file.parent,
                check_activation=False,
            )
        )

    return sources


RULES_JSON_CANDIDATE_LOCATIONS = (
    "rules.json",
    "rules/rules.json",
    ".conditional_rules/rules.json",
)

# Registry `scope` values that install a plugin for every project...
GLOBAL_INSTALL_SCOPES = ("user", "managed")
# ...and the ones that install it for one repo only (named by `projectPath`).
PROJECT_INSTALL_SCOPES = ("project", "local")
# Every scope value we know how to place. Anything else is unplaceable: the
# record is skipped and the unknown value is reported on stderr so a future
# Claude Code scope does not read as "my rules silently vanished".
KNOWN_INSTALL_SCOPES = GLOBAL_INSTALL_SCOPES + PROJECT_INSTALL_SCOPES


def _read_enabled_plugins_section(settings_file: Path) -> dict[str, bool]:
    """Return the `enabledPlugins` map from `settings_file`, or `{}`.

    Tolerant of every realistic missing/malformed shape: file absent,
    empty, unreadable, not JSON, top-level not an object, `enabledPlugins`
    missing, `enabledPlugins` not an object, individual values not bool.
    Anything off-shape is silently dropped — settings are read-only here
    and a broken settings file must never block a tool call.
    """
    if not settings_file.is_file():
        return {}
    try:
        text = settings_file.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    section = data.get("enabledPlugins") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return {}
    return {
        key: value
        for key, value in section.items()
        if isinstance(key, str) and isinstance(value, bool)
    }


def _load_disabled_plugins(
    user_settings_file: Path,
    project_root: Path,
) -> set[str]:
    """Return plugin keys (`<plugin>@<marketplace>`) explicitly disabled.

    Walks `enabledPlugins` from three settings files in precedence order
    (lowest to highest):

      1. `user_settings_file` — usually `~/.claude/settings.json`.
      2. `<project>/.claude/settings.json`.
      3. `<project>/.claude/settings.local.json`.

    A higher-precedence file overrides a lower one for the same plugin
    key; the merged value decides. A plugin appears in the returned set
    iff its effective merged value is `False`. Keys absent from every
    file are not included — callers default to "enabled", matching
    Claude Code's `/plugin disable` semantics: an installed plugin runs
    unless explicitly disabled at some scope.
    """
    files = (
        user_settings_file,
        project_root / ".claude" / "settings.json",
        project_root / ".claude" / "settings.local.json",
    )
    merged: dict[str, bool] = {}
    for path in files:
        merged.update(_read_enabled_plugins_section(path))
    return {key for key, value in merged.items() if value is False}


def _discover_from_registry(
    registry_file: Path,
    project_root: Path,
    disabled_plugin_keys: set[str] | frozenset[str] | None = None,
) -> list[RuleSource]:
    """Walk Claude Code's installed-plugins registry and return rule sources.

    The registry file shape (as of Claude Code's `version: 2`):

        {
          "version": 2,
          "plugins": {
            "<plugin>@<marketplace>": [
              {
                "scope": "user|project|local|managed",
                "projectPath": "<absolute path, on project/local records>",
                "installPath": "<absolute path to version-pinned cache dir>",
                "version": "<semver>",
                ...
              },
              ...
            ],
            ...
          }
        }

    Each record is gated by its own `scope` before `installPath` is even
    looked at, because a record only describes where a plugin is installed
    *for a given scope*:

    - `user` and `managed` — installed for every project; always eligible.
    - `project` and `local` — installed for one specific repo. Eligible only
      when the record carries a non-empty *absolute* `projectPath` that
      resolves to the same directory as `project_root`. Without this check, a
      plugin installed for repo A would inject its rules into every other
      repo. A relative `projectPath` is rejected outright: it would resolve
      against the hook process's cwd, which Claude Code sets to the project
      dir, so a foreign record carrying `"."` would match everywhere. Claude
      Code writes absolute paths, so nothing legitimate is lost.
    - Missing, non-string, or unrecognized `scope` — skipped, with one line
      per offending record on stderr (this is the only skip we log; a
      project/local record pointing at another repo is the routine case and
      logging it would spam every event). We cannot tell whether the record
      applies here, and the gating fails closed: skipping a source never
      blocks a tool call (it only means no rules are injected), whereas
      loading another project's rules is exactly the failure mode this gate
      exists to prevent. That is the opposite posture from malformed settings
      JSON (see `_read_enabled_plugins_section`), where failing open is the
      safe direction.

    Eligible records are still subject to the `disabled_plugin_keys` filter,
    which applies at the plugin-key level (all scopes at once).

    For each eligible record we probe a small set of conventional locations
    inside `installPath` for `rules.json` (see
    `RULES_JSON_CANDIDATE_LOCATIONS`). The first match wins. We deliberately
    do not rglob: a recursive walk can pick up test fixtures or vendored
    files. Records are deduped by resolved `installPath` so a single plugin
    installed at multiple scopes only contributes its rules once.

    `content_root` is set to the directory that contains `rules.json` so
    `content_file` paths resolve correctly regardless of which candidate
    matched. (e.g. for `rules/rules.json`, content files like
    `api-patterns.md` live next to `rules.json` under `rules/`.)

    `disabled_plugin_keys` is the set of `<plugin>@<marketplace>` keys the
    user has flipped to `false` via `/plugin disable` (computed from
    `enabledPlugins` in user/project settings — see
    `_load_disabled_plugins`). Records under those keys are skipped:
    being in the registry only means a plugin is installed, not that the
    user wants it active. The default `None` means "do not filter" and
    is preserved for direct unit tests that don't care about gating.

    The registry is consulted as a hint, not a contract. Errors are logged
    and skipped, never raised: a malformed entry must not block tool calls.
    Stale older versions still on disk under the cache root are ignored
    because the registry only points at the active version.
    """
    sources: list[RuleSource] = []
    try:
        text = registry_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"conditional_rules: failed to read installed plugins registry {registry_file}: {exc}",
            file=sys.stderr,
        )
        return sources

    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        print(
            f"conditional_rules: installed plugins registry {registry_file} "
            f"is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return sources

    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return sources

    try:
        project_root_resolved: Path | None = project_root.resolve()
    except (OSError, ValueError):
        project_root_resolved = None

    seen: set[Path] = set()
    for plugin_key in sorted(plugins.keys()):
        if disabled_plugin_keys and plugin_key in disabled_plugin_keys:
            continue
        records = plugins[plugin_key]
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            scope = record.get("scope")
            if not isinstance(scope, str) or scope not in KNOWN_INSTALL_SCOPES:
                print(
                    f"conditional_rules: installed plugins registry {registry_file} "
                    f"has a record for {plugin_key} with an unusable scope "
                    f"{scope!r}; skipping it",
                    file=sys.stderr,
                )
                continue
            if not _record_applies_to_project(record, project_root_resolved):
                continue
            install_path = record.get("installPath")
            if not isinstance(install_path, str) or not install_path:
                continue
            try:
                install_dir = Path(install_path).resolve()
            except (OSError, ValueError):
                continue
            if install_dir in seen:
                continue
            rules_file = _find_rules_json_in(install_dir)
            if rules_file is None:
                continue
            seen.add(install_dir)
            sources.append(
                RuleSource(
                    rules_file=rules_file,
                    content_root=rules_file.parent,
                    check_activation=True,
                )
            )
    return sources


def _record_applies_to_project(
    record: dict,
    project_root_resolved: Path | None,
) -> bool:
    """True if a registry scope record is in force for the current project.

    `user` and `managed` records install a plugin for every project. `project`
    and `local` records install it for the single repo named by their
    `projectPath`, so they only apply when that path resolves to the project
    the hook is running in.

    Every record we cannot positively place returns False: a missing or
    non-string `scope`, an unrecognized scope value, a project/local record
    whose `projectPath` is missing, non-string, empty, or relative, an
    unresolvable path, or an unresolvable `project_root`. A relative
    `projectPath` would resolve against the hook process's cwd — the project
    dir — and so match every repo. The gate fails closed on purpose; see
    `_discover_from_registry`, which also owns the stderr diagnostic for
    unrecognized scopes (this predicate stays silent so the routine
    "belongs to another repo" skip does not spam every event).
    """
    scope = record.get("scope")
    if not isinstance(scope, str):
        return False
    if scope in GLOBAL_INSTALL_SCOPES:
        return True
    if scope not in PROJECT_INSTALL_SCOPES:
        return False
    if project_root_resolved is None:
        return False
    project_path = record.get("projectPath")
    if not isinstance(project_path, str) or not project_path:
        return False
    if not Path(project_path).is_absolute():
        return False
    try:
        return Path(project_path).resolve() == project_root_resolved
    except (OSError, ValueError):
        return False


def _find_rules_json_in(install_dir: Path) -> Path | None:
    """Return the first `rules.json` found at a conventional location, else None."""
    for candidate in RULES_JSON_CANDIDATE_LOCATIONS:
        path = install_dir / candidate
        if path.is_file():
            return path
    return None


# ---------- Orchestration ----------


def build_output(
    resolved: list[tuple[str, str, str]],
    event_name: str,
) -> dict | None:
    """Build the hook output from a list of (rule_id, body, source_label) triples.

    Emits both the LLM-facing `additionalContext` and a user-facing `systemMessage`
    that lists each rule's source path, one per line.

    `additionalContext` opens with a one-line manifest naming every rule that
    fired. Claude Code caps hook output at 10,000 chars and persists anything
    larger to a file, showing the model only a head preview plus a "Full output
    saved to: <path>" pointer. Putting the manifest first guarantees the preview
    names every active rule even when later rule bodies are pushed past the cap,
    so the model can tell it is missing content and read the persisted file in
    full. `systemMessage` (user-facing only) is unaffected.
    """
    if not resolved:
        return None
    rule_ids = [rid for rid, _, _ in resolved]
    manifest = (
        f"<!-- Conditional rules active ({len(rule_ids)}): {', '.join(rule_ids)}. "
        "If this hook output was truncated and persisted to a file, Read that file in "
        "full now — every rule body below is binding for the current task. -->"
    )
    chunks = [manifest]
    chunks.extend(f"## Rule: {rid}\n{body}" for rid, body, _ in resolved)
    messages = [f"Conditional Rules: Loaded {label}" for _, _, label in resolved]
    return {
        "systemMessage": "\n".join(messages),
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": "\n\n".join(chunks),
        },
    }


def _build_cache_entry(
    rule: Rule,
    source: RuleSource,
    event_name: str,
    tool_name: str | None,
    file_path_rel: str | None,
    now: float,
) -> dict:
    """Assemble the audit-log payload recorded for a single rule activation.

    `is_marketplace_rule` mirrors `RuleSource.check_activation`; project rules
    are the only sources where it is False.
    """
    is_marketplace = source.check_activation
    return {
        "hook_event_name": event_name,
        "tool_name": tool_name if event_name in TOOL_EVENTS else None,
        "is_marketplace_rule": is_marketplace,
        "is_project_rule": not is_marketplace,
        "when_activated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "triggering_file": (file_path_rel if rule.uses_triggering_file_predicates else None),
        "any_file": True if rule.uses_any_file_predicates else None,
    }


def _rule_source_label(rule: Rule, source: RuleSource) -> str:
    """Content-root-relative label for a rule, used in the systemMessage.

    - `content_file` rules → the content-root-relative path to that file.
    - Inline `content` rules → `rules.json#<rule-id>` anchor form.
    """
    content_root = source.content_root.resolve()
    if rule.content_file is not None:
        try:
            return rule.content_file.resolve().relative_to(content_root).as_posix()
        except ValueError:
            return str(rule.content_file)
    try:
        rules_rel = source.rules_file.resolve().relative_to(content_root).as_posix()
    except ValueError:
        rules_rel = str(source.rules_file)
    return f"{rules_rel}#{rule.id}"


def build_block_output(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"conditional_rules hook: {message}",
        }
    }


def handle(
    event_name: str,
    hook_input: dict,
    paths: Paths,
    now: float | None = None,
) -> dict | None:
    """Pure orchestration layer. Returns the JSON output to emit, or None."""
    if event_name not in SUPPORTED_EVENTS:
        return None
    if now is None:
        now = time.time()

    opportunistic_sweep(paths.state_dir, now, STATE_SWEEP_AGE_SECONDS)

    session_id = hook_input.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    # Subagents inherit the parent's session_id but receive their own agent_id.
    # Without scoping the cache by agent_id, a subagent would hit the parent's
    # already-injected entries and skip injection — but the subagent has a fresh
    # context window that never saw the parent's rules.
    agent_id = hook_input.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        cache_file = paths.state_dir / f"{session_id}__{agent_id}.json"
    else:
        cache_file = paths.state_dir / f"{session_id}.json"

    if event_name == "SessionStart":
        wipe_cache(cache_file)

    cached = load_cache(cache_file)

    tool_name: str | None = None
    if event_name in TOOL_EVENTS:
        tn = hook_input.get("tool_name")
        if isinstance(tn, str):
            tool_name = tn
        if tool_name not in TOOL_ALLOWLIST:
            return None

    # Build the evaluation context once; reused for activation_criteria and when conditions.
    if event_name in TOOL_EVENTS:
        tool_input = hook_input.get("tool_input") or {}
        file_path_abs_str = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        file_path_rel = normalize_project_relative(file_path_abs_str, paths.project_root)
        # An unresolvable triggering path (NUL byte -> ValueError, symlink loop
        # -> OSError) degrades to "no triggering file", matching what
        # normalize_project_relative already returned for the same input.
        # triggering_file_* predicates then simply do not match, instead of the
        # whole event dying in run_entry's catch-all.
        file_path_abs: Path | None = None
        if file_path_abs_str:
            try:
                file_path_abs = Path(file_path_abs_str).resolve()
            except (OSError, ValueError):
                file_path_abs = None
    else:
        file_path_abs = None
        file_path_rel = None

    ctx = EvalContext(
        file_path_abs=file_path_abs,
        file_path_rel=file_path_rel,
        project_root=paths.project_root,
    )

    sources = _collect_rule_sources(
        paths.project_root,
        paths.marketplace_dir,
        paths.installed_plugins_file,
        paths.user_settings_file,
    )

    # Gather all candidate (rule, source) pairs across every active source.
    all_candidates: list[tuple[Rule, RuleSource]] = []
    first_config_error: str | None = None

    for source in sources:
        try:
            result = load_rules(source.rules_file, source.content_root)
        except ConfigError as exc:
            if event_name == "PreToolUse" and first_config_error is None:
                first_config_error = str(exc)
            else:
                print(
                    f"conditional_rules: config error at {event_name}: {exc}",
                    file=sys.stderr,
                )
            continue

        if result is None:
            continue

        # For marketplace sources, evaluate activation_criteria when present.
        if source.check_activation and result.activation_criteria is not None:
            try:
                if not evaluate(result.activation_criteria, ctx):
                    continue
            except Exception as exc:
                print(
                    f"conditional_rules: activation_criteria error in {source.rules_file}: {exc}",
                    file=sys.stderr,
                )
                continue

        for rule in result.rules:
            all_candidates.append((rule, source))

    if first_config_error is not None and not all_candidates:
        return build_block_output(first_config_error)

    # Filter by enabled flag, session cache, event routing, and tools.
    eligible: list[tuple[Rule, RuleSource]] = []
    for rule, source in all_candidates:
        if not rule.enabled:
            continue
        if rule.id in cached:
            continue
        if event_name not in rule.reached_events:
            continue
        if event_name in TOOL_EVENTS:
            allowed_tools = rule.tools if rule.tools is not None else TOOL_ALLOWLIST
            if tool_name not in allowed_tools:
                continue
        eligible.append((rule, source))

    if not eligible:
        return None

    resolved: list[tuple[Rule, RuleSource, str]] = []
    for rule, source in eligible:
        try:
            if not evaluate(rule.when, ctx):
                continue
        except Exception as exc:
            print(
                f"conditional_rules: evaluation error in rule {rule.id!r}: {exc}",
                file=sys.stderr,
            )
            continue
        body = rule.resolve_content()
        if body is None:
            continue
        resolved.append((rule, source, body))

    if not resolved:
        return None

    new_entries: dict[str, dict] = {}
    for rule, source, _body in resolved:
        new_entries[rule.id] = _build_cache_entry(
            rule, source, event_name, tool_name, file_path_rel, now
        )
    append_to_cache(cache_file, new_entries)
    ensure_project_state_symlink(cache_file, paths.project_root)

    output_rows = [
        (rule.id, body, _rule_source_label(rule, source)) for rule, source, body in resolved
    ]
    return build_output(output_rows, event_name)


def _detect_plugin_root() -> Path:
    """Resolve the conditional-rules plugin root (used for state_dir only).

    Claude Code exports `CLAUDE_PLUGIN_ROOT` to every hook subprocess. When the
    variable is absent (ad-hoc CLI testing, legacy runs), fall back to the
    script's own location: `<plugin_root>/hooks/conditional_rules/*.py`.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2]


def _detect_project_root(hook_input: dict) -> Path:
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        return Path(env_dir)
    cwd = hook_input.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path.cwd()


def _detect_marketplace_dir() -> Path:
    """Resolve the marketplace directory for legacy rule-source discovery.

    `CLAUDE_MARKETPLACE_DIR` overrides the default (`$HOME/.claude/plugins/marketplaces`).
    Only used when `installed_plugins_file` is missing — see `_collect_rule_sources`.
    Tests use this env var to inject a controlled marketplace without touching
    the user's real plugin installation.
    """
    env_dir = os.environ.get("CLAUDE_MARKETPLACE_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".claude" / "plugins" / "marketplaces"


def _detect_installed_plugins_file() -> Path:
    """Resolve the installed-plugins registry path used by registry-based discovery.

    `CLAUDE_INSTALLED_PLUGINS_FILE` overrides the default
    (`$HOME/.claude/plugins/installed_plugins.json`). Set the env var to a
    non-existent path to opt out of registry-based discovery and force the
    legacy `marketplace_dir` scan (useful for plugin authors who want their
    in-progress rules picked up before they go through the install flow).
    """
    env_file = os.environ.get("CLAUDE_INSTALLED_PLUGINS_FILE")
    if env_file:
        return Path(env_file)
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _detect_user_settings_file() -> Path:
    """Resolve the user-scope settings file used to read `enabledPlugins`.

    `CLAUDE_USER_SETTINGS_FILE` overrides the default
    (`$HOME/.claude/settings.json`). The env var exists primarily so tests
    can inject a controlled `enabledPlugins` set without touching the
    user's real settings; production runs always use the default.
    """
    env_file = os.environ.get("CLAUDE_USER_SETTINGS_FILE")
    if env_file:
        return Path(env_file)
    return Path.home() / ".claude" / "settings.json"


def run_entry(event_name: str, script_name: str) -> int:
    """Shared entry-point body for the three wrapper scripts."""
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"{script_name}: stdin not valid JSON: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"{script_name}: failed to read stdin: {exc}", file=sys.stderr)
        return 0

    try:
        paths = Paths.from_env(hook_input)
        output = handle(event_name, hook_input, paths)
    except Exception as exc:
        print(f"{script_name}: unexpected error: {exc}", file=sys.stderr)
        return 0

    if output is not None:
        json.dump(output, sys.stdout)
    return 0
