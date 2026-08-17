#!/usr/bin/env python3
"""
Tests for the project-level rules feature.

Project rules live at `$CLAUDE_PROJECT_DIR/.claude/conditional_rules/rules.json`
and are loaded unconditionally — `activation_criteria` is ignored for project
sources. Content files resolve relative to the same directory.

Covers:
  - Absent rules.json returns None without error
  - activation_criteria is silently ignored for project sources
  - PreToolUse, PostToolUse, and SessionStart handle flows
  - Dedup within a session and cache wipe on SessionStart
  - enabled=false silences a rule
  - Config errors: deny block on PreToolUse, stderr log on other events
  - content_file resolution from the project conditional_rules dir
  - content_file path-traversal rejection
  - Multiple rules with partial when-condition match
  - any_file_content_regex predicate in a project rule

Marketplace plugin tests (activation_criteria evaluation, entry scripts,
end-to-end multi-plugin scenarios) live in test_conditional_rules_for_marketplace.py.
Cross-source interaction tests live in test_conditional_rules_for_marketplace_and_project.py.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from _test_helpers import _RepoTestCase, _rule, cr


class ProjectRulesTests(_RepoTestCase):
    def _write_project_rules(self, data: dict) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text(json.dumps(data), encoding="utf-8")

    def test_project_rules_applied_regardless_of_activation_criteria(self) -> None:
        # activation_criteria would fail (no .csproj), but project rules ignore it.
        self._write_project_rules(
            {
                "activation_criteria": {"any_file_exists": "*.csproj"},
                "rules": [
                    _rule(
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNotNone(out)

    def test_project_rule_content_file_resolved_from_project_dir(self) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "acme.md").write_text("acme content", encoding="utf-8")
        (project_rules_dir / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "r1",
                            "fires_on_matcher": "SessionStart",
                            "when": {"any_file_exists": "src"},
                            "content_file": "acme.md",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertIn("acme content", out["hookSpecificOutput"]["additionalContext"])

    def test_project_rules_content_file_cannot_escape_project_dir(self) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "r1",
                            "fires_on_matcher": "SessionStart",
                            "when": {"any_file_exists": "src"},
                            "content_file": "../../../secret.md",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(cr.ConfigError, "escapes plugin root"):
            cr.load_rules(project_rules_dir / "rules.json", project_rules_dir)

    # --- Absent rules.json ---

    def test_absent_project_rules_file_returns_none(self) -> None:
        # No rules.json written — handle() must return None without error.
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNone(out)

    # --- PreToolUse / PostToolUse flows ---

    def test_project_rule_fires_on_pretooluse(self) -> None:
        self._write_project_rules(
            {"rules": [_rule(when={"triggering_file_path_glob": "src/*.py"}, content="py rule")]}
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("py rule", out["hookSpecificOutput"]["additionalContext"])

    def test_project_rule_fires_on_posttooluse(self) -> None:
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        fires_on_matcher="PostToolUse",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="post rule",
                    )
                ]
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py", event="PostToolUse")
        out = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_project_rule_when_condition_false_returns_none(self) -> None:
        # Rule is valid and loaded, but the triggering file doesn't match.
        self._write_project_rules(
            {"rules": [_rule(when={"triggering_file_path_glob": "src/*.py"})]}
        )
        self.repo.write_file("tests/test_a.py")
        inp = self.repo.make_input(file_rel="tests/test_a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNone(out)

    # --- enabled=false ---

    def test_project_disabled_rule_does_not_fire(self) -> None:
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        enabled=False,
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                    )
                ]
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNone(out)

    # --- Dedup and session wipe ---

    def test_project_rule_dedup_within_session(self) -> None:
        self._write_project_rules(
            {"rules": [_rule(when={"triggering_file_path_glob": "src/*.py"})]}
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-proj-dedup")
        out1 = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out1)
        out2 = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNone(out2)

    def test_project_sessionstart_wipes_cache_and_reinjects(self) -> None:
        self._write_project_rules(
            {"rules": [_rule(fires_on_matcher="*", when={"any_file_exists": "src"})]}
        )
        self.repo.write_file("src/a.py")
        session_id = "sess-proj-wipe"

        # First: PreToolUse fires and caches the rule.
        inp_pre = self.repo.make_input(file_rel="src/a.py", session_id=session_id)
        self.assertIsNotNone(cr.handle("PreToolUse", inp_pre, self.repo.paths))

        # SessionStart wipes the cache → same rule re-injects.
        inp_session = self.repo.make_input(
            file_rel=None, event="SessionStart", session_id=session_id
        )
        out = cr.handle("SessionStart", inp_session, self.repo.paths)
        self.assertIsNotNone(out)

    # --- Config error handling ---

    def test_project_config_error_on_pretooluse_produces_deny_block(self) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text("not json at all", encoding="utf-8")
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_project_config_error_on_sessionstart_logs_and_does_not_block(self) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text("not json at all", encoding="utf-8")
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNone(out)
        self.assertIn("config error", err.getvalue())

    def test_project_config_error_on_posttooluse_logs_and_does_not_block(self) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text("not json at all", encoding="utf-8")
        inp = self.repo.make_input(file_rel="src/a.py", event="PostToolUse")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNone(out)
        self.assertIn("config error", err.getvalue())

    # --- Multiple rules and predicates ---

    def test_project_multiple_rules_partial_match(self) -> None:
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        id="r-py",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="python rule",
                    ),
                    _rule(
                        id="r-cs",
                        when={"triggering_file_path_glob": "src/*.cs"},
                        content="csharp rule",
                    ),
                ]
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("python rule", ac)
        self.assertNotIn("csharp rule", ac)

    def test_project_rule_audit_entry_marks_is_project_rule(self) -> None:
        self._write_project_rules(
            {"rules": [_rule(when={"triggering_file_path_glob": "src/*.py"})]}
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-proj-audit")
        cr.handle("PreToolUse", inp, self.repo.paths)
        cache_file = self.repo.paths.state_dir / "sess-proj-audit.json"
        entry = cr.load_cache(cache_file)["r1"]
        self.assertFalse(entry["is_marketplace_rule"])
        self.assertTrue(entry["is_project_rule"])
        self.assertEqual(entry["triggering_file"], "src/a.py")

    def test_project_rule_with_any_file_content_regex(self) -> None:
        self.repo.write_project_file("pyproject.toml", "[project]\nname = 'acme'\n")
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        fires_on_matcher="SessionStart",
                        when={
                            "any_file_content_regex": {
                                "path": "pyproject.toml",
                                "pattern": r"name = 'acme'",
                            }
                        },
                        content="acme rules",
                    )
                ]
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertIn("acme rules", out["hookSpecificOutput"]["additionalContext"])


# ---------- Project-side .state/ symlink (troubleshooting helper) ----------


class ProjectStateSymlinkTests(_RepoTestCase):
    """When a project has its own rules.json, the engine creates a symlink
    `<project>/.claude/conditional_rules/.state/<session>.json` pointing at
    the canonical cache file in the plugin cache, so users can `cat` the
    audit log without leaving their repo."""

    def _write_project_rules(self) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text(
            json.dumps({"rules": [_rule()]}), encoding="utf-8"
        )

    def _symlink_path(self, session_id: str) -> Path:
        return (
            self.repo.project_root
            / ".claude"
            / "conditional_rules"
            / ".state"
            / f"{session_id}.json"
        )

    def _canonical_cache(self, session_id: str) -> Path:
        return self.repo.paths.state_dir / f"{session_id}.json"

    def test_symlink_created_when_project_rules_exist(self) -> None:
        self._write_project_rules()
        self.repo.write_file("src/a.py")
        cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="link-1"),
            self.repo.paths,
        )
        link = self._symlink_path("link-1")
        self.assertTrue(link.is_symlink(), f"expected symlink at {link}")
        # Resolves to the canonical cache file.
        self.assertEqual(link.resolve(), self._canonical_cache("link-1").resolve())
        # Reading through the symlink returns the audit JSON.
        data = json.loads(link.read_text(encoding="utf-8"))
        self.assertIn("r1", data["injected"])

    def test_no_symlink_when_project_rules_absent(self) -> None:
        # Marketplace rules only — no project rules.json.
        self.repo.write_rules({"rules": [_rule()]})
        self.repo.write_file("src/a.py")
        cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="no-link"),
            self.repo.paths,
        )
        symlink_dir = self.repo.project_root / ".claude" / "conditional_rules" / ".state"
        self.assertFalse(
            symlink_dir.exists(),
            "should not create the .state directory when project has no rules.json",
        )

    def test_symlink_idempotent_on_repeated_invocations(self) -> None:
        self._write_project_rules()
        self.repo.write_file("src/a.py")
        for _ in range(3):
            cr.handle(
                "PreToolUse",
                self.repo.make_input(file_rel="src/a.py", session_id="idemp"),
                self.repo.paths,
            )
        link = self._symlink_path("idemp")
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self._canonical_cache("idemp").resolve())

    def test_stale_symlink_pointing_elsewhere_is_replaced(self) -> None:
        self._write_project_rules()
        link = self._symlink_path("stale")
        link.parent.mkdir(parents=True, exist_ok=True)
        # Plant a symlink pointing somewhere else.
        bogus_target = self.repo.project_root / "bogus.json"
        link.symlink_to(bogus_target)
        self.assertEqual(os.readlink(link), str(bogus_target))

        self.repo.write_file("src/a.py")
        cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="stale"),
            self.repo.paths,
        )
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self._canonical_cache("stale").resolve())

    def test_existing_regular_file_at_symlink_path_is_not_clobbered(self) -> None:
        self._write_project_rules()
        link = self._symlink_path("regular")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text('{"keep": "this"}', encoding="utf-8")

        self.repo.write_file("src/a.py")
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            cr.handle(
                "PreToolUse",
                self.repo.make_input(file_rel="src/a.py", session_id="regular"),
                self.repo.paths,
            )
        # Original file untouched.
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(encoding="utf-8"), '{"keep": "this"}')
        self.assertIn("refusing to replace non-symlink", captured.getvalue())

    def test_symlink_creation_failure_is_logged_but_does_not_block(self) -> None:
        self._write_project_rules()
        self.repo.write_file("src/a.py")
        captured = io.StringIO()
        with (
            mock.patch("sys.stderr", captured),
            mock.patch(
                "pathlib.Path.symlink_to",
                side_effect=OSError("permission denied"),
            ),
        ):
            out = cr.handle(
                "PreToolUse",
                self.repo.make_input(file_rel="src/a.py", session_id="fail"),
                self.repo.paths,
            )
        # Hook output is unaffected — the rule still injects.
        self.assertIsNotNone(out)
        self.assertIn("default content", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("failed to create state symlink", captured.getvalue())

    def test_symlink_works_for_session_start_event(self) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        _rule(
                            when={"any_file_exists": "."},
                            fires_on_matcher="SessionStart",
                        )
                    ]
                }
            ),
            encoding="utf-8",
        )
        cr.handle(
            "SessionStart",
            self.repo.make_input(file_rel=None, event="SessionStart", session_id="sess-link"),
            self.repo.paths,
        )
        link = self._symlink_path("sess-link")
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self._canonical_cache("sess-link").resolve())


# ---------- Cross-project simulation ----------


class CrossProjectScopingTests(_RepoTestCase):
    """Project rules in repo A must not fire for tool ops on files in repo B.

    Reproduces the dotnet-template / NDjango.RestFramework scenario: a user
    starts Claude Code in project A (which has its own conditional rules) and
    asks the model to read a file in unrelated project B sitting elsewhere on
    disk. Project A's rules must not match B's files — for any predicate type.

    Mirrors a realistic dotnet-template-style ruleset: a SessionStart `main-
    rules` gate, a path-glob `api-patterns` rule, and a content-regex
    `hangfire-jobs` rule whose path-glob branch wouldn't match B's path but
    whose content regex would.
    """

    USER_RULES: ClassVar[dict] = {
        "rules": [
            {
                "id": "main-rules",
                "fires_on_matcher": "SessionStart",
                "when": {"any_file_exists": "."},
                "content": "session-opening brief",
            },
            {
                "id": "api-patterns",
                "when": {
                    "any_of": [
                        {"triggering_file_path_glob": "**/Controllers/**/*.cs"},
                        {"triggering_file_path_glob": "**/V1/**/*.cs"},
                    ]
                },
                "content": "controller conventions",
            },
            {
                "id": "hangfire-jobs",
                "when": {
                    "any_of": [
                        {"triggering_file_path_glob": "src/Jobs/**/*.cs"},
                        {"triggering_file_content_regex": r"using Hangfire;"},
                    ]
                },
                "content": "hangfire conventions",
            },
        ]
    }

    def setUp(self) -> None:
        super().setUp()
        # Project A: ${project_root} carries the rules (the "dotnet-template").
        cr_dir = self.repo.project_root / ".claude" / "conditional_rules"
        cr_dir.mkdir(parents=True, exist_ok=True)
        (cr_dir / "rules.json").write_text(json.dumps(self.USER_RULES), encoding="utf-8")

        # Project B: a separate tmp dir for the "NDjango.RestFramework" repo.
        self._other_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._other_tmp.cleanup)
        self.other_root = Path(self._other_tmp.name).resolve()
        # Same path shape the user described: src/Controllers/V1/...Controller.cs,
        # with content that matches both api-patterns (by path) and hangfire-jobs
        # (by content). The path is irrelevant to the engine since it's outside
        # project_root, but using a path that *would* match if we forgot to
        # gate makes the test's intent explicit.
        external = self.other_root / "src" / "Controllers" / "V1" / "OnlineOrdersController.cs"
        external.parent.mkdir(parents=True)
        external.write_text(
            "using System;\n"
            "using Hangfire;\n"
            "namespace NDjango.RestFramework.Controllers.V1;\n"
            "public class OnlineOrdersController {}\n",
            encoding="utf-8",
        )
        self.external_file = external

    def _hook_input(self, *, file_path: Path | None, event: str, session_id: str) -> dict:
        tool_input: dict = {}
        if file_path is not None:
            tool_input["file_path"] = str(file_path)
        return {
            "session_id": session_id,
            "hook_event_name": event,
            "tool_name": "Read" if event != "SessionStart" else "",
            "tool_input": tool_input,
            "cwd": str(self.repo.project_root),
        }

    def _fired_rule_ids(self, output: dict | None) -> set[str]:
        if output is None:
            return set()
        ctx = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        return {
            line.removeprefix("## Rule: ")
            for line in ctx.splitlines()
            if line.startswith("## Rule: ")
        }

    def test_session_start_fires_main_rules_in_project_a(self) -> None:
        out = cr.handle(
            "SessionStart",
            self._hook_input(file_path=None, event="SessionStart", session_id="cross-ss"),
            self.repo.paths,
        )
        self.assertEqual(self._fired_rule_ids(out), {"main-rules"})

    def test_inside_project_a_controller_fires_path_and_content_rules(self) -> None:
        # Sanity check: project A's rules still match files inside project A.
        inside = self.repo.write_file(
            "src/Controllers/V1/PersonsController.cs",
            "using Hangfire;\n",
        )
        out = cr.handle(
            "PreToolUse",
            self._hook_input(file_path=inside, event="PreToolUse", session_id="cross-inside"),
            self.repo.paths,
        )
        self.assertEqual(
            self._fired_rule_ids(out),
            {"api-patterns", "hangfire-jobs"},
        )

    def test_outside_project_b_controller_does_not_fire_path_globs(self) -> None:
        # api-patterns uses triggering_file_path_glob only; gated by
        # file_path_rel is None.
        out = cr.handle(
            "PreToolUse",
            self._hook_input(
                file_path=self.external_file,
                event="PreToolUse",
                session_id="cross-outside-path",
            ),
            self.repo.paths,
        )
        self.assertNotIn("api-patterns", self._fired_rule_ids(out))

    def test_outside_project_b_content_regex_does_not_fire(self) -> None:
        # Regression: hangfire-jobs has an `any_of` whose path branch can't
        # match (file is outside) and whose content_regex branch *would* match
        # (`using Hangfire;` is in B's controller). Pre-fix this rule fired;
        # post-fix it does not.
        out = cr.handle(
            "PreToolUse",
            self._hook_input(
                file_path=self.external_file,
                event="PreToolUse",
                session_id="cross-outside-content",
            ),
            self.repo.paths,
        )
        self.assertNotIn("hangfire-jobs", self._fired_rule_ids(out))

    def test_outside_project_b_no_rule_fires_on_tool_event(self) -> None:
        # Composite assertion: no rule from project A fires for B's file.
        out = cr.handle(
            "PreToolUse",
            self._hook_input(
                file_path=self.external_file,
                event="PreToolUse",
                session_id="cross-outside-all",
            ),
            self.repo.paths,
        )
        self.assertEqual(self._fired_rule_ids(out), set())


if __name__ == "__main__":
    unittest.main()
