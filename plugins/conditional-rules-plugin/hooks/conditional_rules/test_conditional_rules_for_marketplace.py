#!/usr/bin/env python3
"""
Tests for the conditional-rules marketplace plugin system.

Covers: config validation, field validation (tools, fires_on_matcher,
description, enabled), activation_criteria, all PreToolUse/PostToolUse/
SessionStart handle flows, dedup, entry scripts, the JSON Schema file, the
create_structure helper, and end-to-end scenarios that exercise
marketplace-only rule sources.

Engine tests (evaluate(), normalize_project_relative(), output builders) live
in test_conditional_rules_for_engine.py.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_helpers import (
    HERE,
    _EntryScriptRunner,
    _make_e2e_input,
    _RepoTestCase,
    _rule,
    cr,
    create_structure,
)

# ---------- Config validation ----------


class ConfigValidationTests(_RepoTestCase):
    def test_missing_rules_file_returns_none(self) -> None:
        self.assertIsNone(cr.load_rules(self.repo.rules_file, self.repo.content_root))

    def test_valid_minimal_config(self) -> None:
        self.repo.write_rules({"rules": [_rule()]})
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(len(result.rules), 1)
        self.assertEqual(result.rules[0].id, "r1")
        self.assertEqual(result.rules[0].content, "default content")
        self.assertTrue(result.rules[0].enabled)
        self.assertIsNone(result.rules[0].description)
        self.assertIsNone(result.rules[0].tools)
        self.assertEqual(result.rules[0].fires_on_matcher, "PreToolUse")
        self.assertEqual(result.rules[0].reached_events, frozenset({"PreToolUse"}))

    def test_schema_key_is_accepted_and_ignored(self) -> None:
        self.repo.write_rules(
            {
                "$schema": "./rules.schema.json",
                "rules": [_rule()],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(len(result.rules), 1)

    def test_unknown_top_level_key_is_rejected(self) -> None:
        self.repo.write_rules({"rules": [_rule()], "extra_key": 1})
        with self.assertRaisesRegex(cr.ConfigError, "unknown top-level key"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_version_key_is_rejected_as_unknown(self) -> None:
        """The version field was removed; it now belongs to the unknown-top-level set."""
        self.repo.write_rules({"version": 1, "rules": [_rule()]})
        with self.assertRaisesRegex(cr.ConfigError, "unknown top-level key"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_content_as_list_is_joined_with_newline(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(content=["line one", "line two", "line three"])],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].content, "line one\nline two\nline three")

    def test_content_file_is_validated_shape_only(self) -> None:
        # File need NOT exist at config-load time (lazy resolution).
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"triggering_file_path_glob": "src/*.py"},
                        "content_file": "missing.md",
                    }
                ],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertIsNone(result.rules[0].content)
        # Resolved relative to content_root.
        self.assertEqual(
            result.rules[0].content_file,
            (self.repo.content_root / "missing.md").resolve(),
        )

    def test_content_file_resolves_against_content_root(self) -> None:
        self.repo.write_plugin_file("api.md", "api rules\n")
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"triggering_file_path_glob": "src/*.py"},
                        "content_file": "api.md",
                    }
                ],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].resolve_content(), "api rules\n")

    def test_content_file_accepts_nested_path(self) -> None:
        """A path like `custom/sample.md` must resolve from content_root."""
        target = self.repo.write_plugin_file("custom/sample.md", "custom body\n")
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"any_file_exists": "src"},
                        "fires_on_matcher": "SessionStart",
                        "content_file": "custom/sample.md",
                    }
                ],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].content_file, target.resolve())
        self.assertEqual(result.rules[0].resolve_content(), "custom body\n")

    def test_content_file_resolve_returns_none_when_missing(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"triggering_file_path_glob": "src/*.py"},
                        "content_file": "nope.md",
                    }
                ],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        with mock.patch("sys.stderr"):
            self.assertIsNone(result.rules[0].resolve_content())

    def test_content_file_outside_content_root_is_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"triggering_file_path_glob": "src/*.py"},
                        "content_file": "../secret.md",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "escapes plugin root"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_invalid_json_raises(self) -> None:
        self.repo.write_rules_raw("{ not valid json")
        with self.assertRaisesRegex(cr.ConfigError, "not valid JSON"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_missing_rules_list_raises(self) -> None:
        self.repo.write_rules({})
        with self.assertRaisesRegex(cr.ConfigError, "'rules' field must be a list"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_duplicate_id_raises(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(id="dup"),
                    _rule(id="dup", when={"triggering_file_path_glob": "*.js"}),
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "duplicated"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_rule_missing_id_raises(self) -> None:
        self.repo.write_rules(
            {
                "rules": [{"when": {"triggering_file_path_glob": "*.py"}, "content": "a"}],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, r"rules\[0\]\.id"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_rule_both_content_and_content_file_raises(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"triggering_file_path_glob": "*.py"},
                        "content": "a",
                        "content_file": "b.md",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "exactly one of 'content' or 'content_file'"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_rule_neither_content_nor_content_file_raises(self) -> None:
        self.repo.write_rules(
            {
                "rules": [{"id": "r1", "when": {"triggering_file_path_glob": "*.py"}}],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "exactly one of 'content' or 'content_file'"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_rule_with_unknown_field_raises(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(unknown_field="x")],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "unknown field"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_unknown_predicate_raises_with_location(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"all_of": [{"triggering_file_pathglob": "*.py"}]})],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, r"rules\[0\]\.when\.all_of\[0\].*unknown key"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_invalid_regex_raises_at_compile_time(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"triggering_file_path_regex": "[unclosed"})],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "not a valid regex"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_multi_key_condition_raises(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={
                            "triggering_file_path_glob": "*.py",
                            "triggering_file_content_regex": ".+",
                        }
                    )
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "must have exactly one key"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_empty_all_of_raises(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"all_of": []})],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "non-empty list"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)


# ---------- any_file_content_regex predicate ----------


class AnyFileContentRegexTests(_RepoTestCase):
    def test_object_shape_missing_path(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"any_file_content_regex": {"pattern": ".+"}})],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "path must be a non-empty string"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_object_shape_missing_pattern(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"any_file_content_regex": {"path": "a.txt"}})],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "pattern must be a non-empty string"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_non_object_argument_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"any_file_content_regex": "a.txt"})],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "must be an object"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_path_escape_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={
                            "any_file_content_regex": {
                                "path": "../secret.txt",
                                "pattern": ".+",
                            },
                        }
                    )
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "must be a relative path"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_absolute_path_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={
                            "any_file_content_regex": {
                                "path": "/etc/passwd",
                                "pattern": ".+",
                            },
                        }
                    )
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "must be a relative path"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_any_file_exists_escape_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={"any_file_exists": "../outside"},
                        fires_on_matcher="SessionStart",
                    )
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "must be a relative path"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_invalid_pattern_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={
                            "any_file_content_regex": {
                                "path": "pyproject.toml",
                                "pattern": "[bad-regex",
                            }
                        }
                    )
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "not a valid regex"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)


# ---------- tools field ----------


class ToolsFieldTests(_RepoTestCase):
    def test_default_absent_behaves_as_all_three(self) -> None:
        self.repo.write_rules({"rules": [_rule()]})
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertIsNone(result.rules[0].tools)

    def test_rule_with_tools_read_fires_on_read_only(self) -> None:
        self.repo.write_rules({"rules": [_rule(tools=["Read"])]})
        self.repo.write_file("src/a.py")
        inp_read = self.repo.make_input(file_rel="src/a.py", tool_name="Read")
        inp_edit = self.repo.make_input(
            file_rel="src/a.py",
            tool_name="Edit",
            session_id="sess-edit",
        )
        self.assertIsNotNone(cr.handle("PreToolUse", inp_read, self.repo.paths))
        self.assertIsNone(cr.handle("PreToolUse", inp_edit, self.repo.paths))

    def test_invalid_tool_name_config_error(self) -> None:
        self.repo.write_rules({"rules": [_rule(tools=["Bash"])]})
        with self.assertRaisesRegex(cr.ConfigError, "not a valid tool name"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_non_list_tools_rejected(self) -> None:
        self.repo.write_rules({"rules": [_rule(tools="Edit")]})
        with self.assertRaisesRegex(cr.ConfigError, "must be a list"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_empty_tools_list_rejected(self) -> None:
        self.repo.write_rules({"rules": [_rule(tools=[])]})
        with self.assertRaisesRegex(cr.ConfigError, "non-empty list"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_tools_with_sessionstart_only_matcher_is_config_error(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={"any_file_exists": "tests"},
                        tools=["Edit"],
                        fires_on_matcher="SessionStart",
                    )
                ],
            }
        )
        with self.assertRaisesRegex(
            cr.ConfigError,
            "tools is set but fires_on_matcher .* does not fire on any tool-bearing event",
        ):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)


# ---------- fires_on_matcher field ----------


class FiresOnMatcherTests(_RepoTestCase):
    def test_default_absent_equals_pretoolsuse(self) -> None:
        self.repo.write_rules({"rules": [_rule()]})
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].reached_events, frozenset({"PreToolUse"}))

    def test_tier1_star_matches_every_supported_event(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"any_file_exists": "src"}, fires_on_matcher="*")],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].reached_events, frozenset(cr.SUPPORTED_EVENTS))

    def test_tier1_empty_string_matches_all(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"any_file_exists": "src"}, fires_on_matcher="")],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].reached_events, frozenset(cr.SUPPORTED_EVENTS))

    def test_tier2_exact_match(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="PreToolUse")],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].reached_events, frozenset({"PreToolUse"}))

    def test_tier2_alternation(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="PreToolUse|PostToolUse")],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].reached_events, frozenset({"PreToolUse", "PostToolUse"}))

    def test_tier2_unreachable_is_config_error(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="Foo")],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "does not match any supported event"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_tier3_regex_matches(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="^Pre.*")],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].reached_events, frozenset({"PreToolUse"}))

    def test_tier3_invalid_regex_is_config_error(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="[unclosed")],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "fires_on_matcher is not a valid regex"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_triggering_predicate_plus_sessionstart_is_config_error(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={"triggering_file_path_glob": "src/*.py"},
                        fires_on_matcher="*",
                    )
                ],
            }
        )
        with self.assertRaisesRegex(
            cr.ConfigError, "uses triggering_file_.* predicate but fires_on_matcher"
        ):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_runtime_filter_rule_posttoolsuse_only_skipped_on_pre(self) -> None:
        # Rule requires PostToolUse — a PreToolUse invocation should return no output.
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="PostToolUse")],
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py")
        self.assertIsNone(cr.handle("PreToolUse", inp, self.repo.paths))

    def test_matches_event_helper(self) -> None:
        self.assertTrue(cr.matches_event(None, "PreToolUse"))
        self.assertTrue(cr.matches_event("*", "PreToolUse"))
        self.assertTrue(cr.matches_event("", "PreToolUse"))
        self.assertTrue(cr.matches_event("PreToolUse", "PreToolUse"))
        self.assertFalse(cr.matches_event("PreToolUse", "PostToolUse"))
        self.assertTrue(cr.matches_event("Pre|Post", "Pre"))
        self.assertTrue(cr.matches_event("^Pre.*", "PreToolUse"))
        self.assertFalse(cr.matches_event("^Post.*", "PreToolUse"))


# ---------- description field ----------


class DescriptionFieldTests(_RepoTestCase):
    def test_valid_description_accepted(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(description="a human note")],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertEqual(result.rules[0].description, "a human note")

    def test_non_string_description_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(description=123)],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "description must be a non-empty string"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_empty_string_description_rejected(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(description="")],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "description must be a non-empty string"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_description_surfaces_in_error_messages(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        description="Acme legacy-pattern warning",
                        when={"all_of": [{"triggering_file_pathglob": "*.py"}]},
                    )
                ],
            }
        )
        with self.assertRaisesRegex(
            cr.ConfigError, r"rules\[0\] \('Acme legacy-pattern warning'\).*unknown key"
        ):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)


# ---------- enabled field ----------


class EnabledFieldTests(_RepoTestCase):
    def test_absent_defaults_to_true(self) -> None:
        self.repo.write_rules({"rules": [_rule()]})
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertTrue(result.rules[0].enabled)

    def test_disabled_rule_does_not_fire_on_any_event(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        enabled=False,
                        when={"any_file_exists": "src"},
                        fires_on_matcher="*",
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py")
        inp["session_id"] = "sess-pre"
        self.assertIsNone(cr.handle("PreToolUse", inp, self.repo.paths))
        inp["session_id"] = "sess-post"
        self.assertIsNone(cr.handle("PostToolUse", inp, self.repo.paths))
        inp["session_id"] = "sess-session"
        self.assertIsNone(cr.handle("SessionStart", inp, self.repo.paths))

    def test_non_boolean_enabled_rejected(self) -> None:
        for bad in ["true", "T", 1, 0]:
            with self.subTest(bad=bad):
                self.repo.write_rules({"rules": [_rule(enabled=bad)]})
                with self.assertRaisesRegex(cr.ConfigError, "enabled must be a JSON boolean"):
                    cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_disabled_broken_rule_still_raises_config_error(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        enabled=False,
                        when={"triggering_file_path_regex": "[unclosed"},
                    )
                ],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "not a valid regex"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)


# ---------- Activation criteria ----------


class ActivationCriteriaTests(_RepoTestCase):
    def test_activation_criteria_accepted_as_top_level_key(self) -> None:
        self.repo.write_rules(
            {
                "activation_criteria": {"any_file_exists": "src"},
                "rules": [_rule()],
            }
        )
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.activation_criteria)
        self.assertEqual(len(result.rules), 1)

    def test_no_activation_criteria_returns_none_in_result(self) -> None:
        self.repo.write_rules({"rules": [_rule()]})
        result = cr.load_rules(self.repo.rules_file, self.repo.content_root)
        self.assertIsNone(result.activation_criteria)

    def test_invalid_activation_criteria_raises(self) -> None:
        self.repo.write_rules(
            {
                "activation_criteria": {"bad_predicate": "value"},
                "rules": [_rule()],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "unknown key"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_activation_criteria_true_applies_marketplace_rules(self) -> None:
        self.repo.write_file("app.csproj", "<Project/>")
        self.repo.write_rules(
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

    def test_activation_criteria_false_skips_marketplace_rules(self) -> None:
        # No .csproj file in the project — activation_criteria evaluates False.
        self.repo.write_rules(
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
        self.assertIsNone(out)

    def test_no_activation_criteria_always_applies(self) -> None:
        self.repo.write_rules(
            {
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

    def test_activation_criteria_with_glob_pattern(self) -> None:
        self.repo.write_file("solution.sln", "")
        self.repo.write_rules(
            {
                "activation_criteria": {
                    "any_of": [
                        {"any_file_exists": "*.sln"},
                        {"any_file_exists": "*.csproj"},
                    ]
                },
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

    def test_activation_criteria_with_triggering_file_predicate_raises(self) -> None:
        for predicate in (
            "triggering_file_path_glob",
            "triggering_file_path_regex",
            "triggering_file_content_regex",
        ):
            with self.subTest(predicate=predicate):
                self.repo.write_rules(
                    {
                        "activation_criteria": {predicate: ".*"},
                        "rules": [_rule()],
                    }
                )
                with self.assertRaisesRegex(cr.ConfigError, r"triggering_file_.*any_file_"):
                    cr.load_rules(self.repo.rules_file, self.repo.content_root)

    def test_activation_criteria_triggering_file_nested_in_combinator_raises(self) -> None:
        self.repo.write_rules(
            {
                "activation_criteria": {
                    "all_of": [
                        {"any_file_exists": "src"},
                        {"triggering_file_path_glob": "*.py"},
                    ]
                },
                "rules": [_rule()],
            }
        )
        with self.assertRaisesRegex(cr.ConfigError, "triggering_file_"):
            cr.load_rules(self.repo.rules_file, self.repo.content_root)


# ---------- Handle — PreToolUse flow ----------


class HandlePreToolUseTests(_RepoTestCase):
    def _write_standard_rules(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "acme-legacy",
                        "when": {
                            "all_of": [
                                {"triggering_file_path_glob": "src/service/acme.py"},
                                {"triggering_file_content_regex": r".+bla"},
                            ]
                        },
                        "content": "refactor the legacy bla pattern",
                    },
                    {
                        "id": "api-convention",
                        "when": {
                            "any_of": [
                                {"triggering_file_path_glob": "src/api/**/*.py"},
                                {"triggering_file_path_glob": "src/routes/**/*.py"},
                            ]
                        },
                        "content": "use @route decorators",
                    },
                ],
            }
        )

    def test_non_allowlisted_tool_returns_none(self) -> None:
        self._write_standard_rules()
        inp = self.repo.make_input(file_rel="src/api/x.py", tool_name="Bash")
        self.assertIsNone(cr.handle("PreToolUse", inp, self.repo.paths))

    def test_missing_rules_file_returns_none(self) -> None:
        inp = self.repo.make_input(file_rel="src/api/x.py")
        self.assertIsNone(cr.handle("PreToolUse", inp, self.repo.paths))

    def test_matching_rule_is_injected(self) -> None:
        self._write_standard_rules()
        self.repo.write_file("src/api/users.py")
        inp = self.repo.make_input(file_rel="src/api/users.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## Rule: api-convention", ac)
        self.assertIn("use @route decorators", ac)

    def test_inline_content_rule_system_message_points_to_rules_json(self) -> None:
        self._write_standard_rules()
        self.repo.write_file("src/api/users.py")
        inp = self.repo.make_input(file_rel="src/api/users.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertEqual(
            out["systemMessage"],
            "Conditional Rules: Loaded rules.json#api-convention",
        )

    def test_content_file_rule_system_message_points_to_content_file(self) -> None:
        self.repo.write_plugin_file("test.md", "body")
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"triggering_file_path_glob": "src/*.py"},
                        "content_file": "test.md",
                    }
                ],
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertEqual(
            out["systemMessage"],
            "Conditional Rules: Loaded test.md",
        )

    def test_multiple_rules_produce_multi_line_system_message(self) -> None:
        self.repo.write_plugin_file("a.md", "a")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="inline",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="c",
                    ),
                    {
                        "id": "from-file",
                        "when": {"triggering_file_path_glob": "src/*.py"},
                        "content_file": "a.md",
                    },
                ],
            }
        )
        self.repo.write_file("src/x.py")
        inp = self.repo.make_input(file_rel="src/x.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertEqual(
            out["systemMessage"],
            "Conditional Rules: Loaded rules.json#inline\nConditional Rules: Loaded a.md",
        )

    def test_second_match_is_deduped_same_session(self) -> None:
        self._write_standard_rules()
        self.repo.write_file("src/api/users.py")
        inp = self.repo.make_input(file_rel="src/api/users.py")
        first = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(first)
        self.repo.write_file("src/api/orders.py")
        inp2 = self.repo.make_input(file_rel="src/api/orders.py")
        second = cr.handle("PreToolUse", inp2, self.repo.paths)
        self.assertIsNone(second)

    def test_different_sessions_each_get_injection(self) -> None:
        self._write_standard_rules()
        self.repo.write_file("src/api/users.py")
        inp1 = self.repo.make_input(file_rel="src/api/users.py", session_id="sess-A")
        inp2 = self.repo.make_input(file_rel="src/api/users.py", session_id="sess-B")
        self.assertIsNotNone(cr.handle("PreToolUse", inp1, self.repo.paths))
        self.assertIsNotNone(cr.handle("PreToolUse", inp2, self.repo.paths))

    def test_content_based_trigger_requires_content_match(self) -> None:
        self._write_standard_rules()
        self.repo.write_file("src/service/acme.py", content="no match here")
        inp = self.repo.make_input(file_rel="src/service/acme.py")
        self.assertIsNone(cr.handle("PreToolUse", inp, self.repo.paths))

    def test_content_based_trigger_hits_when_content_matches(self) -> None:
        self._write_standard_rules()
        self.repo.write_file("src/service/acme.py", content="yes blabla here")
        inp = self.repo.make_input(file_rel="src/service/acme.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertIn("acme-legacy", out["hookSpecificOutput"]["additionalContext"])

    def test_multiple_matching_rules_concatenated(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r1",
                        when={"triggering_file_path_glob": "src/**/*.py"},
                        content="c1",
                    ),
                    _rule(
                        id="r2",
                        when={"triggering_file_path_regex": r".+\.py$"},
                        content="c2",
                    ),
                ],
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## Rule: r1", ac)
        self.assertIn("## Rule: r2", ac)

    def test_config_error_produces_deny_output_on_pretoolsuse(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="bad",
                        when={"all_of": [{"triggering_file_pathglob": "*.py"}]},
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("triggering_file_pathglob", hso["permissionDecisionReason"])
        self.assertIn("rules[0].when.all_of[0]", hso["permissionDecisionReason"])

    def test_runtime_error_on_target_file_does_not_block(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"triggering_file_content_regex": r".+"})],
            }
        )
        (self.repo.project_root / "src" / "dir_as_file").mkdir(exist_ok=True)
        inp = self.repo.make_input(file_rel="src/dir_as_file")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNone(out)

    def test_file_outside_project_path_predicates_fail(self) -> None:
        self._write_standard_rules()
        with tempfile.TemporaryDirectory() as other:
            external = Path(other) / "service" / "acme.py"
            external.parent.mkdir(parents=True)
            external.write_text("bla")
            inp = {
                "session_id": "sess-x",
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(external)},
                "cwd": str(self.repo.project_root),
            }
            self.assertIsNone(cr.handle("PreToolUse", inp, self.repo.paths))

    def test_file_outside_project_content_regex_predicate_does_not_fire(self) -> None:
        # Regression: a rule whose only path-bearing predicate is
        # triggering_file_content_regex (no path glob/regex) used to fire
        # for files outside CLAUDE_PROJECT_DIR because read_content() reads
        # file_path_abs directly. Pin the corrected behavior end-to-end.
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="content-only",
                        when={"triggering_file_content_regex": r"using Hangfire;"},
                        content="hangfire conventions",
                    )
                ],
            }
        )
        with tempfile.TemporaryDirectory() as other:
            external = Path(other) / "src" / "Jobs" / "OutsideJob.cs"
            external.parent.mkdir(parents=True)
            external.write_text("using Hangfire;\nclass OutsideJob {}\n")
            inp = {
                "session_id": "sess-content-outside",
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": str(external)},
                "cwd": str(self.repo.project_root),
            }
            self.assertIsNone(cr.handle("PreToolUse", inp, self.repo.paths))

    def test_missing_content_file_does_not_block_and_does_not_cache(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"triggering_file_path_glob": "src/*.py"},
                        "content_file": "missing.md",
                    }
                ],
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py")
        with mock.patch("sys.stderr"):
            out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNone(out)
        cache_file = self.repo.paths.state_dir / "sess-1.json"
        self.assertEqual(cr.load_cache(cache_file), {})

        self.repo.write_plugin_file("missing.md", "now here\n")
        out2 = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out2)
        self.assertIn("now here", out2["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(set(cr.load_cache(cache_file)), {"r1"})

    def test_unresolvable_triggering_path_degrades_to_no_triggering_file(self) -> None:
        """A `file_path` that cannot be resolved must not take the event down.

        `Path.resolve()` raises `ValueError` on an embedded NUL (Python 3.14)
        and `OSError` on e.g. a symlink loop. Both degrade to "no triggering
        file": `triggering_file_*` predicates simply do not match, while
        `any_file_*` rules still inject. Letting the exception escape would
        surface as `unexpected error` in run_entry and silence every rule.
        """
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="needs-triggering-file",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="triggering body",
                    ),
                    _rule(
                        id="context-only",
                        when={"any_file_exists": "src"},
                        content="context body",
                    ),
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py")
        inp["tool_input"]["file_path"] = f"{self.repo.project_root}/src/a\x00b.py"

        out = cr.handle("PreToolUse", inp, self.repo.paths)

        assert out is not None
        hso = out["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("context body", hso["additionalContext"])
        self.assertNotIn("triggering body", hso["additionalContext"])

    def test_unresolvable_triggering_path_records_no_triggering_file_in_audit(
        self,
    ) -> None:
        """The audit entry for the rule that did fire carries no triggering
        file — it never had one."""
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="context-only",
                        when={"any_file_exists": "src"},
                        content="context body",
                    ),
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py")
        inp["tool_input"]["file_path"] = f"{self.repo.project_root}/src/a\x00b.py"

        self.assertIsNotNone(cr.handle("PreToolUse", inp, self.repo.paths))

        entry = cr.load_cache(self.repo.paths.state_dir / "sess-1.json")["context-only"]
        self.assertIsNone(entry["triggering_file"])
        self.assertTrue(entry["any_file"])

    def test_cache_hit_fast_path_skips_evaluation(self) -> None:
        self._write_standard_rules()
        self.repo.write_file("src/api/users.py")
        inp = self.repo.make_input(file_rel="src/api/users.py")
        cr.handle("PreToolUse", inp, self.repo.paths)
        cache_file = self.repo.paths.state_dir / "sess-1.json"
        cr.append_to_cache(cache_file, {"acme-legacy": {"hook_event_name": "PreToolUse"}})

        with mock.patch("conditional_rules.evaluate") as eval_mock:
            out = cr.handle("PreToolUse", inp, self.repo.paths)
            self.assertIsNone(out)
            eval_mock.assert_not_called()


# ---------- PostToolUse flow ----------


class HandlePostToolUseTests(_RepoTestCase):
    def test_rule_with_posttoolsuse_fires_on_post(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="PostToolUse")],
            }
        )
        self.repo.write_file("src/a.py", "body")
        inp = self.repo.make_input(
            file_rel="src/a.py",
            session_id="sess-post",
            event="PostToolUse",
        )
        out = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_post_config_error_logs_and_does_not_emit(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"all_of": [{"triggering_file_pathglob": "*.py"}]})],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py", event="PostToolUse")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNone(out)
        self.assertIn("config error at PostToolUse", err.getvalue())

    def test_posttooluse_non_allowlisted_tool_returns_none(self) -> None:
        self.repo.write_rules({"rules": [_rule(fires_on_matcher="PostToolUse")]})
        inp = self.repo.make_input(file_rel="src/a.py", event="PostToolUse", tool_name="Bash")
        out = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNone(out)

    def test_posttooluse_missing_rules_file_returns_none(self) -> None:
        inp = self.repo.make_input(file_rel="src/a.py", event="PostToolUse")
        out = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNone(out)

    def test_posttooluse_dedup_within_same_event(self) -> None:
        self.repo.write_rules({"rules": [_rule(fires_on_matcher="PostToolUse")]})
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(
            file_rel="src/a.py", session_id="sess-post-dedup", event="PostToolUse"
        )
        out1 = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out1)
        out2 = cr.handle("PostToolUse", inp, self.repo.paths)
        self.assertIsNone(out2)

    def test_posttooluse_sees_post_write_file_content(self) -> None:
        # The spec guarantees PostToolUse reads the post-disk state of the triggering file.
        # Rule only matches "updated content", not "initial content".
        self.repo.write_file("src/a.py", "initial content")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        fires_on_matcher="PostToolUse",
                        when={"triggering_file_content_regex": r"updated content"},
                        content="rule fired after write",
                    )
                ],
            }
        )

        # File still has initial content → rule condition is false.
        inp1 = self.repo.make_input(
            file_rel="src/a.py", session_id="sess-post-v1", event="PostToolUse"
        )
        self.assertIsNone(cr.handle("PostToolUse", inp1, self.repo.paths))

        # Simulate the Write tool completing: update the file on disk.
        self.repo.write_file("src/a.py", "updated content")

        # New session (bypass dedup) — PostToolUse now reads updated content → rule fires.
        inp2 = self.repo.make_input(
            file_rel="src/a.py", session_id="sess-post-v2", event="PostToolUse"
        )
        out = cr.handle("PostToolUse", inp2, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertIn("rule fired after write", out["hookSpecificOutput"]["additionalContext"])


# ---------- SessionStart flow ----------


class HandleSessionStartTests(_RepoTestCase):
    def test_sessionstart_rule_injects_when_any_file_matches(self) -> None:
        self.repo.write_file("pyproject.toml", "content\n")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={"any_file_exists": "pyproject.toml"},
                        fires_on_matcher="SessionStart",
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_sessionstart_wipes_previous_cache(self) -> None:
        self.repo.write_file("pyproject.toml", "content\n")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={"any_file_exists": "pyproject.toml"},
                        fires_on_matcher="SessionStart",
                    )
                ],
            }
        )
        # Pre-populate the cache with some stale entry.
        cache_file = self.repo.paths.state_dir / "sess-wipe.json"
        cache_file.write_text(
            '{"injected": {"stale-rule": {"hook_event_name": "PreToolUse"}}}',
            encoding="utf-8",
        )

        inp = self.repo.make_input(
            file_rel=None,
            event="SessionStart",
            session_id="sess-wipe",
        )
        out = cr.handle("SessionStart", inp, self.repo.paths)
        # After SessionStart the cache should contain only the newly-injected rule,
        # not the stale one.
        self.assertIsNotNone(out)
        self.assertEqual(set(cr.load_cache(cache_file)), {"r1"})

    def test_sessionstart_injection_deduped_against_later_pretoolsuse(self) -> None:
        self.repo.write_file("pyproject.toml", "content\n")
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={"any_file_exists": "pyproject.toml"},
                        fires_on_matcher="*",
                    )
                ],
            }
        )
        inp_session = self.repo.make_input(
            file_rel=None,
            event="SessionStart",
            session_id="sess-dedup",
        )
        self.assertIsNotNone(cr.handle("SessionStart", inp_session, self.repo.paths))

        # Same rule id should NOT re-inject on PreToolUse in the same session.
        inp_pre = self.repo.make_input(
            file_rel="src/a.py",
            session_id="sess-dedup",
        )
        self.assertIsNone(cr.handle("PreToolUse", inp_pre, self.repo.paths))

    def test_sessionstart_skips_rule_with_missing_content_file(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    {
                        "id": "r1",
                        "when": {"any_file_exists": "src"},
                        "content_file": "missing.md",
                        "fires_on_matcher": "SessionStart",
                    }
                ],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        with mock.patch("sys.stderr"):
            out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNone(out)

    def test_sessionstart_config_error_does_not_block(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(when={"all_of": [{"triggering_file_pathglob": "*.py"}]})],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNone(out)
        self.assertIn("config error at SessionStart", err.getvalue())


# ---------- Dedup across events ----------


class DedupAcrossEventsTests(_RepoTestCase):
    def test_pre_or_post_injects_at_most_once(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="PreToolUse|PostToolUse")],
            }
        )
        self.repo.write_file("src/a.py", "body")
        inp_pre = self.repo.make_input(file_rel="src/a.py", session_id="sess-X")
        out1 = cr.handle("PreToolUse", inp_pre, self.repo.paths)
        self.assertIsNotNone(out1)

        inp_post = self.repo.make_input(
            file_rel="src/a.py",
            session_id="sess-X",
            event="PostToolUse",
        )
        out2 = cr.handle("PostToolUse", inp_post, self.repo.paths)
        self.assertIsNone(out2)

    def test_pre_first_then_post_same_rule_noop(self) -> None:
        """If pre injected first, a subsequent post for the same id is a no-op."""
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="*")],
            }
        )
        self.repo.write_file("src/a.py", "body")
        inp_pre = self.repo.make_input(file_rel="src/a.py", session_id="sess-Y")
        self.assertIsNotNone(cr.handle("PreToolUse", inp_pre, self.repo.paths))

        inp_post = self.repo.make_input(
            file_rel="src/a.py",
            session_id="sess-Y",
            event="PostToolUse",
        )
        self.assertIsNone(cr.handle("PostToolUse", inp_post, self.repo.paths))


# ---------- Subagent cache scoping ----------


class SubagentCacheScopingTests(_RepoTestCase):
    """Subagents inherit session_id but receive a distinct agent_id.

    The dedup cache is keyed by both so a subagent's fresh context window
    still gets its rules injected, while the parent's already-injected set
    continues to dedup correctly.
    """

    def _write_standard_rule(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r1",
                        when={"triggering_file_path_glob": "src/**/*.py"},
                        content="parent or subagent reminder",
                    )
                ],
            }
        )

    def test_subagent_gets_own_injection_despite_parent_already_injected(self) -> None:
        self._write_standard_rule()
        self.repo.write_file("src/a.py")

        parent_in = self.repo.make_input(file_rel="src/a.py", session_id="sess-1")
        self.assertIsNotNone(cr.handle("PreToolUse", parent_in, self.repo.paths))

        subagent_in = self.repo.make_input(
            file_rel="src/a.py", session_id="sess-1", agent_id="agent-A"
        )
        self.assertIsNotNone(cr.handle("PreToolUse", subagent_in, self.repo.paths))

        parent_cache = self.repo.paths.state_dir / "sess-1.json"
        subagent_cache = self.repo.paths.state_dir / "sess-1__agent-A.json"
        self.assertTrue(parent_cache.is_file())
        self.assertTrue(subagent_cache.is_file())

    def test_two_subagents_in_same_session_get_independent_caches(self) -> None:
        self._write_standard_rule()
        self.repo.write_file("src/a.py")

        a_in = self.repo.make_input(file_rel="src/a.py", session_id="sess-1", agent_id="agent-A")
        b_in = self.repo.make_input(file_rel="src/a.py", session_id="sess-1", agent_id="agent-B")
        self.assertIsNotNone(cr.handle("PreToolUse", a_in, self.repo.paths))
        self.assertIsNotNone(cr.handle("PreToolUse", b_in, self.repo.paths))

        self.assertTrue((self.repo.paths.state_dir / "sess-1__agent-A.json").is_file())
        self.assertTrue((self.repo.paths.state_dir / "sess-1__agent-B.json").is_file())

    def test_subagent_dedup_still_holds_within_its_own_cache(self) -> None:
        self._write_standard_rule()
        self.repo.write_file("src/a.py")

        first = self.repo.make_input(file_rel="src/a.py", session_id="sess-1", agent_id="agent-A")
        self.assertIsNotNone(cr.handle("PreToolUse", first, self.repo.paths))

        self.repo.write_file("src/b.py")
        second = self.repo.make_input(file_rel="src/b.py", session_id="sess-1", agent_id="agent-A")
        self.assertIsNone(cr.handle("PreToolUse", second, self.repo.paths))

    def test_sessionstart_wipes_only_the_parent_cache(self) -> None:
        """SessionStart in Claude Code only fires for the parent session.

        A parent's SessionStart must not invalidate subagent caches — they
        are owned by separate, ongoing subagent contexts.
        """
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-tool",
                        when={"triggering_file_path_glob": "src/**/*.py"},
                        fires_on_matcher="PreToolUse",
                        content="x",
                    )
                ],
            }
        )
        self.repo.write_file("src/a.py")

        # Subagent injects its rule first.
        sub_in = self.repo.make_input(file_rel="src/a.py", session_id="sess-1", agent_id="agent-A")
        self.assertIsNotNone(cr.handle("PreToolUse", sub_in, self.repo.paths))
        subagent_cache = self.repo.paths.state_dir / "sess-1__agent-A.json"
        self.assertTrue(subagent_cache.is_file())

        # Parent's SessionStart fires (e.g. /clear). It should only touch the
        # parent's cache file; the subagent's cache must survive.
        parent_session_in = self.repo.make_input(
            file_rel=None, session_id="sess-1", event="SessionStart"
        )
        cr.handle("SessionStart", parent_session_in, self.repo.paths)
        self.assertTrue(subagent_cache.is_file())

    def test_empty_string_agent_id_is_treated_as_absent(self) -> None:
        """Defense-in-depth: an empty agent_id must not produce a `sess-1__.json` file."""
        self._write_standard_rule()
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-1", agent_id="")
        self.assertIsNotNone(cr.handle("PreToolUse", inp, self.repo.paths))
        self.assertTrue((self.repo.paths.state_dir / "sess-1.json").is_file())
        self.assertFalse((self.repo.paths.state_dir / "sess-1__.json").exists())


# ---------- Cache audit log ----------


_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class CacheAuditLogTests(_RepoTestCase):
    """The session cache records per-rule activation metadata."""

    def _cache_file(self, session_id: str) -> Path:
        return self.repo.paths.state_dir / f"{session_id}.json"

    def test_pretooluse_entry_has_all_required_fields(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-pre",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="x",
                    )
                ],
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-audit", tool_name="Edit")
        cr.handle("PreToolUse", inp, self.repo.paths)
        cache = cr.load_cache(self._cache_file("sess-audit"))
        self.assertIn("r-pre", cache)
        entry = cache["r-pre"]
        self.assertEqual(entry["hook_event_name"], "PreToolUse")
        self.assertEqual(entry["tool_name"], "Edit")
        self.assertTrue(entry["is_marketplace_rule"])
        self.assertFalse(entry["is_project_rule"])
        self.assertRegex(entry["when_activated"], _TIMESTAMP_RE)
        self.assertEqual(entry["triggering_file"], "src/a.py")
        self.assertIsNone(entry["any_file"])

    def test_sessionstart_entry_has_null_tool_name(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-session",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                    )
                ],
            }
        )
        inp = self.repo.make_input(
            file_rel=None, event="SessionStart", session_id="sess-audit-sess"
        )
        cr.handle("SessionStart", inp, self.repo.paths)
        cache = cr.load_cache(self._cache_file("sess-audit-sess"))
        entry = cache["r-session"]
        self.assertEqual(entry["hook_event_name"], "SessionStart")
        self.assertIsNone(entry["tool_name"])
        self.assertIsNone(entry["triggering_file"])
        self.assertTrue(entry["any_file"])

    def test_entry_marks_both_predicates_when_when_uses_both(self) -> None:
        # Rule combines a triggering predicate and an any_file predicate.
        self.repo.write_file("src/a.py")
        self.repo.write_file("pyproject.toml", "x")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-both",
                        when={
                            "all_of": [
                                {"triggering_file_path_glob": "src/*.py"},
                                {"any_file_exists": "pyproject.toml"},
                            ]
                        },
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-both")
        cr.handle("PreToolUse", inp, self.repo.paths)
        entry = cr.load_cache(self._cache_file("sess-both"))["r-both"]
        self.assertEqual(entry["triggering_file"], "src/a.py")
        self.assertTrue(entry["any_file"])

    def test_cache_preserves_order_and_metadata_across_invocations(self) -> None:
        # Rule-A fires on PreToolUse first, then rule-B fires on a later
        # PostToolUse — the on-disk order must reflect that history AND the
        # earlier entry's metadata (timestamp, event) must survive the merge
        # untouched.
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="rule-A",
                        when={"triggering_file_path_glob": "src/*.py"},
                    ),
                    _rule(
                        id="rule-B",
                        when={"triggering_file_path_glob": "src/*.py"},
                        fires_on_matcher="PostToolUse",
                    ),
                ],
            }
        )
        sid = "sess-order"
        t_pre, t_post = 1714305840.0, 1714305900.0
        cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id=sid),
            self.repo.paths,
            now=t_pre,
        )
        snapshot_a = cr.load_cache(self._cache_file(sid))["rule-A"]

        cr.handle(
            "PostToolUse",
            self.repo.make_input(file_rel="src/a.py", event="PostToolUse", session_id=sid),
            self.repo.paths,
            now=t_post,
        )

        cache = cr.load_cache(self._cache_file(sid))
        self.assertEqual(list(cache.keys()), ["rule-A", "rule-B"])
        # rule-A's audit row is unchanged by the rule-B merge.
        self.assertEqual(cache["rule-A"], snapshot_a)
        self.assertEqual(cache["rule-A"]["hook_event_name"], "PreToolUse")
        # rule-B got the later timestamp.
        self.assertEqual(
            cache["rule-B"]["when_activated"],
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_post)),
        )

    def test_posttooluse_entry_records_event_and_tool_name(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-post",
                        when={"triggering_file_path_glob": "src/*.py"},
                        fires_on_matcher="PostToolUse",
                    )
                ],
            }
        )
        self.repo.write_file("src/a.py", "body")
        inp = self.repo.make_input(
            file_rel="src/a.py",
            session_id="sess-audit-post",
            tool_name="Write",
            event="PostToolUse",
        )
        cr.handle("PostToolUse", inp, self.repo.paths)
        entry = cr.load_cache(self._cache_file("sess-audit-post"))["r-post"]
        self.assertEqual(entry["hook_event_name"], "PostToolUse")
        self.assertEqual(entry["tool_name"], "Write")
        self.assertEqual(entry["triggering_file"], "src/a.py")
        self.assertIsNone(entry["any_file"])

    def test_triggering_file_flag_set_when_predicate_nested_in_not(self) -> None:
        # Doc claim: triggering_file is populated whenever the rule's `when`
        # tree references a triggering_file_* predicate, including under
        # combinators like `not`.
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-not",
                        when={"not": {"triggering_file_path_glob": "tests/**/*.py"}},
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-not")
        cr.handle("PreToolUse", inp, self.repo.paths)
        entry = cr.load_cache(self._cache_file("sess-not"))["r-not"]
        self.assertEqual(entry["triggering_file"], "src/a.py")
        self.assertIsNone(entry["any_file"])

    def test_any_file_flag_set_when_predicate_nested_in_any_of(self) -> None:
        # Mirror of the above for any_file_*: a predicate buried in `any_of`
        # still flips the flag.
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-any-of",
                        when={
                            "any_of": [
                                {"triggering_file_path_glob": "src/*.py"},
                                {"any_file_exists": "src"},
                            ]
                        },
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-any-of")
        cr.handle("PreToolUse", inp, self.repo.paths)
        entry = cr.load_cache(self._cache_file("sess-any-of"))["r-any-of"]
        self.assertEqual(entry["triggering_file"], "src/a.py")
        self.assertTrue(entry["any_file"])

    def test_when_activated_reflects_provided_now(self) -> None:
        # `handle()` accepts `now` for deterministic timestamps; the cache
        # entry must use it (formatted in local time, per the docs) instead
        # of calling time.time() itself.
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-now",
                        when={"triggering_file_path_glob": "src/*.py"},
                    )
                ],
            }
        )
        self.repo.write_file("src/a.py")
        frozen = 1714305840.0
        expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(frozen))
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-now")
        cr.handle("PreToolUse", inp, self.repo.paths, now=frozen)
        entry = cr.load_cache(self._cache_file("sess-now"))["r-now"]
        self.assertEqual(entry["when_activated"], expected)

    def test_load_cache_returns_empty_on_malformed_json(self) -> None:
        cache_file = self._cache_file("sess-malformed")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(cr.load_cache(cache_file), {})

    def test_load_cache_returns_empty_on_legacy_list_shape(self) -> None:
        # The pre-audit-log cache schema stored a list of rule ids. Loading
        # such a file must not crash; it is treated as empty so the new code
        # re-fires every rule cleanly.
        cache_file = self._cache_file("sess-legacy")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text('{"injected": ["legacy-rule"]}', encoding="utf-8")
        self.assertEqual(cr.load_cache(cache_file), {})

    def test_cache_file_is_pretty_printed_with_indent_2(self) -> None:
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="r-fmt",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="body",
                    )
                ],
            }
        )
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py", session_id="sess-fmt")
        cr.handle("PreToolUse", inp, self.repo.paths)
        raw = self._cache_file("sess-fmt").read_text(encoding="utf-8")
        self.assertIn("\n", raw)
        # `indent=2` produces two-space indented keys.
        self.assertIn('  "injected"', raw)
        self.assertIn('    "r-fmt"', raw)


# ---------- Entry-script stdin/stdout ----------


class EntryScriptsTests(_RepoTestCase):
    def _runner(self, script_name: str) -> _EntryScriptRunner:
        return _EntryScriptRunner(
            script_name,
            self.repo.plugin_root,
            self.repo.project_root,
            self.repo.marketplace_dir,
        )

    def test_pre_script_prints_json_on_match(self) -> None:
        self.repo.write_rules({"rules": [_rule()]})
        self.repo.write_file("src/a.py")
        inp = self.repo.make_input(file_rel="src/a.py", session_id="pre-sess")
        rc, out, err = self._runner("conditional_rules_pre.py").run(json.dumps(inp))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_pre_script_garbage_stdin_exits_zero(self) -> None:
        rc, out, err = self._runner("conditional_rules_pre.py").run("not json at all")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("not valid JSON", err)

    def test_post_script_prints_json_on_match(self) -> None:
        self.repo.write_rules(
            {
                "rules": [_rule(fires_on_matcher="PostToolUse")],
            }
        )
        self.repo.write_file("src/a.py", "body")
        inp = self.repo.make_input(
            file_rel="src/a.py",
            session_id="post-sess",
            event="PostToolUse",
        )
        rc, out, err = self._runner("conditional_rules_post.py").run(json.dumps(inp))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_session_script_wipes_and_injects(self) -> None:
        self.repo.write_file("pyproject.toml", "ok")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        when={"any_file_exists": "pyproject.toml"},
                        fires_on_matcher="SessionStart",
                    )
                ],
            }
        )
        cache_file = self.repo.paths.state_dir / "sess-entry.json"
        cache_file.write_text(
            '{"injected": {"stale": {"hook_event_name": "PreToolUse"}}}',
            encoding="utf-8",
        )
        inp = self.repo.make_input(
            file_rel=None,
            event="SessionStart",
            session_id="sess-entry",
        )
        rc, out, err = self._runner("conditional_rules_session.py").run(json.dumps(inp))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        # Old "stale" should be gone after the wipe.
        self.assertEqual(set(cr.load_cache(cache_file)), {"r1"})


# ---------- JSON Schema file ----------


class JSONSchemaFileTests(unittest.TestCase):
    def test_schema_file_parses_as_valid_json(self) -> None:
        schema_path = HERE / "rules.schema.json"
        self.assertTrue(schema_path.is_file(), f"schema not found at {schema_path}")
        data = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("$defs", data)
        self.assertNotIn("version", data.get("properties", {}))


# ---------- create_structure helper ----------


class CreateStructureTests(unittest.TestCase):
    def test_builds_nested_tree_and_returns_file_count(self) -> None:
        structure = {
            "a.yaml": "one",
            "b.yaml": "two",
            "infra": {
                "c.yaml": "three",
                "d.yaml": "four",
                "another": {
                    "e.yaml": "five",
                    "f.yaml": "six",
                },
            },
            "g.yaml": "seven",
            "h.yaml": "eight",
            "test": {"i.yaml": "nine"},
        }
        with create_structure(structure) as (root, created):
            self.assertEqual(len(created), 9)
            self.assertTrue((root / "a.yaml").is_file())
            self.assertTrue((root / "infra" / "another" / "e.yaml").is_file())
            self.assertEqual((root / "g.yaml").read_text(), "seven")
            retained_root = root
        self.assertFalse(retained_root.exists(), "root should be removed on exit")

    def test_base_dir_override_does_not_clean_up(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            with create_structure({"x.txt": "hi"}, base_dir=Path(parent) / "sub") as (
                root,
                created,
            ):
                self.assertEqual(len(created), 1)
                self.assertTrue((root / "x.txt").is_file())
            self.assertTrue((Path(parent) / "sub" / "x.txt").is_file())

    def test_non_string_non_dict_value_raises(self) -> None:
        with self.assertRaises(TypeError), create_structure({"bad": 123}):  # type: ignore[dict-item]
            pass


# ---------- End-to-end tests (marketplace-only layouts) ----------


class MarketplaceEndToEndTests(unittest.TestCase):
    """Exercise the full hook against realistic marketplace-only layouts."""

    def test_full_layout_injects_matching_rule_and_dedups_on_second_call(self) -> None:
        rules_body = json.dumps(
            {
                "rules": [
                    {
                        "id": "api-route-convention",
                        "when": {
                            "any_of": [
                                {"triggering_file_path_glob": "src/api/**/*.py"},
                                {"triggering_file_path_glob": "src/routes/**/*.py"},
                            ]
                        },
                        "content_file": "api-route-convention.md",
                    },
                    {
                        "id": "acme-legacy",
                        "when": {
                            "all_of": [
                                {"triggering_file_path_glob": "src/service/acme.py"},
                                {"triggering_file_content_regex": r".+bla"},
                            ]
                        },
                        "content": "acme legacy guidance",
                    },
                ],
            }
        )
        structure = {
            "plugin": {},
            "marketplace": {
                "test-plugin": {
                    "rules.json": rules_body,
                    "api-route-convention.md": "API convention body",
                },
            },
            "project": {
                "src": {
                    "api": {"users.py": "def list_users(): ...\n"},
                    "service": {"acme.py": "x = 'no-legacy-pattern-here'\n"},
                },
            },
        }
        with create_structure(structure) as (root, _created):
            plugin_root = root / "plugin"
            project_root = root / "project"
            marketplace_dir = root / "marketplace"
            paths = cr.Paths.for_test(plugin_root, project_root, marketplace_dir=marketplace_dir)

            out1 = cr.handle(
                "PreToolUse",
                _make_e2e_input(project_root, "src/api/users.py"),
                paths,
            )
            self.assertIsNotNone(out1)
            ac = out1["hookSpecificOutput"]["additionalContext"]
            self.assertIn("## Rule: api-route-convention", ac)
            self.assertIn("API convention body", ac)
            self.assertNotIn("acme-legacy", ac)

            out2 = cr.handle(
                "PreToolUse",
                _make_e2e_input(project_root, "src/api/orders.py"),
                paths,
            )
            self.assertIsNone(out2)

            out3 = cr.handle(
                "PreToolUse",
                _make_e2e_input(project_root, "src/service/acme.py"),
                paths,
            )
            self.assertIsNone(out3)

            (project_root / "src" / "service" / "acme.py").write_text("legacy blablabla\n")
            out4 = cr.handle(
                "PreToolUse",
                _make_e2e_input(project_root, "src/service/acme.py"),
                paths,
            )
            self.assertIsNotNone(out4)
            self.assertIn("acme-legacy", out4["hookSpecificOutput"]["additionalContext"])

    def test_sessionstart_hook_restores_injection_on_clear(self) -> None:
        rules_body = json.dumps(
            {
                "rules": [
                    _rule(
                        id="r1",
                        when={"any_file_exists": "src"},
                        fires_on_matcher="*",
                        content="rule body",
                    )
                ],
            }
        )
        structure = {
            "plugin": {},
            "marketplace": {
                "test-plugin": {"rules.json": rules_body},
            },
            "project": {"src": {"a.py": "pass\n"}},
        }
        with create_structure(structure) as (root, _created):
            plugin_root = root / "plugin"
            project_root = root / "project"
            marketplace_dir = root / "marketplace"
            paths = cr.Paths.for_test(plugin_root, project_root, marketplace_dir=marketplace_dir)
            sid = "e2e-reset"

            inp_pre = _make_e2e_input(project_root, "src/a.py", session_id=sid)
            first = cr.handle("PreToolUse", inp_pre, paths)
            self.assertIsNotNone(first)
            self.assertIsNone(cr.handle("PreToolUse", inp_pre, paths))

            # Simulate /clear: SessionStart fires.
            inp_session = _make_e2e_input(project_root, None, session_id=sid, event="SessionStart")
            restart = cr.handle("SessionStart", inp_session, paths)
            self.assertIsNotNone(restart)
            self.assertIn("rule body", restart["hookSpecificOutput"]["additionalContext"])

    def test_realistic_monorepo_layout_only_matches_intended_files(self) -> None:
        rules_body = json.dumps(
            {
                "rules": [
                    {
                        "id": "strict-typing-in-core",
                        "when": {
                            "all_of": [
                                {
                                    "any_of": [
                                        {"triggering_file_path_glob": "src/core/**/*.py"},
                                        {"triggering_file_path_glob": "src/domain/**/*.py"},
                                    ]
                                },
                                {"not": {"triggering_file_path_glob": "**/generated/**"}},
                            ]
                        },
                        "content": "strict typing required",
                    }
                ],
            }
        )
        structure = {
            "plugin": {},
            "marketplace": {
                "test-plugin": {"rules.json": rules_body},
            },
            "project": {
                "src": {
                    "core": {
                        "models.py": "class X: pass\n",
                        "generated": {"proto.py": "# generated\n"},
                    },
                    "domain": {"svc.py": "class S: pass\n"},
                    "api": {"handler.py": "def h(): ...\n"},
                    "utils": {"io.py": "def read(): ...\n"},
                },
                "tests": {"test_core.py": "def test(): ...\n"},
            },
        }
        expected = {
            "src/core/models.py": True,
            "src/domain/svc.py": True,
            "src/core/generated/proto.py": False,
            "src/api/handler.py": False,
            "src/utils/io.py": False,
            "tests/test_core.py": False,
        }
        with create_structure(structure) as (root, _created):
            plugin_root = root / "plugin"
            project_root = root / "project"
            marketplace_dir = root / "marketplace"
            paths = cr.Paths.for_test(plugin_root, project_root, marketplace_dir=marketplace_dir)
            for idx, (file_rel, should_match) in enumerate(expected.items()):
                inp = _make_e2e_input(project_root, file_rel, session_id=f"e2e-matrix-{idx}")
                out = cr.handle("PreToolUse", inp, paths)
                if should_match:
                    self.assertIsNotNone(out, f"{file_rel} should have matched")
                    self.assertIn(
                        "strict-typing-in-core",
                        out["hookSpecificOutput"]["additionalContext"],
                        f"{file_rel} output missing rule",
                    )
                else:
                    self.assertIsNone(out, f"{file_rel} unexpectedly matched: {out}")

    def test_three_rules_different_dispatch_each_fires_correctly(self) -> None:
        """Three rules with different `fires_on_matcher` / `tools` — verify dispatch."""
        rules_body = json.dumps(
            {
                "rules": [
                    _rule(
                        id="pre-only-read",
                        when={"triggering_file_path_glob": "src/*.py"},
                        tools=["Read"],
                        fires_on_matcher="PreToolUse",
                        content="pre only read",
                    ),
                    _rule(
                        id="post-only",
                        when={"triggering_file_path_glob": "src/*.py"},
                        fires_on_matcher="PostToolUse",
                        content="post only",
                    ),
                    _rule(
                        id="session-start",
                        when={"any_file_exists": "src"},
                        fires_on_matcher="SessionStart",
                        content="session only",
                    ),
                ],
            }
        )
        structure = {
            "plugin": {},
            "marketplace": {
                "test-plugin": {"rules.json": rules_body},
            },
            "project": {"src": {"a.py": "x = 1\n"}},
        }
        with create_structure(structure) as (root, _created):
            plugin_root = root / "plugin"
            project_root = root / "project"
            marketplace_dir = root / "marketplace"
            paths = cr.Paths.for_test(plugin_root, project_root, marketplace_dir=marketplace_dir)

            # PreToolUse with Edit -> tools filter excludes pre-only-read.
            inp_pre_edit = _make_e2e_input(
                project_root, "src/a.py", session_id="s-edit", tool="Edit"
            )
            self.assertIsNone(cr.handle("PreToolUse", inp_pre_edit, paths))

            # PreToolUse with Read -> pre-only-read fires.
            inp_pre_read = _make_e2e_input(
                project_root, "src/a.py", session_id="s-read", tool="Read"
            )
            out_pre = cr.handle("PreToolUse", inp_pre_read, paths)
            self.assertIsNotNone(out_pre)
            self.assertIn("pre only read", out_pre["hookSpecificOutput"]["additionalContext"])

            # PostToolUse -> post-only fires.
            inp_post = _make_e2e_input(
                project_root,
                "src/a.py",
                session_id="s-post",
                event="PostToolUse",
            )
            out_post = cr.handle("PostToolUse", inp_post, paths)
            self.assertIsNotNone(out_post)
            self.assertIn("post only", out_post["hookSpecificOutput"]["additionalContext"])

            # SessionStart -> session-start fires.
            inp_session = _make_e2e_input(
                project_root,
                None,
                session_id="s-session",
                event="SessionStart",
            )
            out_session = cr.handle("SessionStart", inp_session, paths)
            self.assertIsNotNone(out_session)
            self.assertIn("session only", out_session["hookSpecificOutput"]["additionalContext"])

    def test_activation_criteria_filters_marketplace_plugin(self) -> None:
        """Marketplace plugin with activation_criteria is only applied when it matches."""
        rules_body_csharp = json.dumps(
            {
                "activation_criteria": {"any_file_exists": "*.csproj"},
                "rules": [
                    _rule(
                        id="csharp-rule",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="csharp conventions",
                    )
                ],
            }
        )
        rules_body_generic = json.dumps(
            {
                "rules": [
                    _rule(
                        id="generic-rule",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="generic conventions",
                    )
                ],
            }
        )
        structure = {
            "plugin": {},
            "marketplace": {
                "csharp-plugin": {"rules.json": rules_body_csharp},
                "generic-plugin": {"rules.json": rules_body_generic},
            },
            "project": {"src": {"a.py": "pass\n"}},
        }
        with create_structure(structure) as (root, _created):
            plugin_root = root / "plugin"
            project_root = root / "project"
            marketplace_dir = root / "marketplace"
            paths = cr.Paths.for_test(plugin_root, project_root, marketplace_dir=marketplace_dir)

            # No .csproj → csharp-plugin skipped; generic-plugin applies.
            out = cr.handle(
                "SessionStart",
                _make_e2e_input(project_root, None, event="SessionStart"),
                paths,
            )
            self.assertIsNotNone(out)
            ac = out["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("csharp conventions", ac)
            self.assertIn("generic conventions", ac)


if __name__ == "__main__":
    unittest.main()
