#!/usr/bin/env python3
"""
Tests for the core conditional-rules engine.

Covers the standalone algorithmic functions that are independent of any
rules.json source or handle() orchestration flow:
  - evaluate() + EvalContext (predicate and combinator evaluation)
  - normalize_project_relative() (path normalization)
  - build_output() / build_block_output() (output shape builders)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_helpers import _RepoTestCase, cr

# ---------- Predicate & combinator evaluation ----------


class EvaluatorTests(_RepoTestCase):
    def _ctx(self, file_rel: str | None, content: str | None = None) -> cr.EvalContext:
        abs_path = None
        if file_rel is not None:
            p = self.repo.write_file(file_rel, content if content is not None else "")
            abs_path = p.resolve()
        return cr.EvalContext(
            file_path_abs=abs_path,
            file_path_rel=file_rel,
            project_root=self.repo.project_root,
        )

    def test_path_glob_basic(self) -> None:
        ctx = self._ctx("src/service/acme.py")
        self.assertTrue(cr.evaluate({"triggering_file_path_glob": "src/service/acme.py"}, ctx))
        self.assertFalse(cr.evaluate({"triggering_file_path_glob": "src/service/other.py"}, ctx))

    def test_path_glob_double_star_recursive(self) -> None:
        ctx = self._ctx("src/a/b/c.py")
        self.assertTrue(cr.evaluate({"triggering_file_path_glob": "src/**/*.py"}, ctx))

    def test_path_regex(self) -> None:
        ctx = self._ctx("src/service/acme.py")
        self.assertTrue(cr.evaluate({"triggering_file_path_regex": r".+/service/.+\.py$"}, ctx))
        self.assertFalse(cr.evaluate({"triggering_file_path_regex": r"^tests/"}, ctx))

    def test_content_regex_reads_file(self) -> None:
        ctx = self._ctx("src/acme.py", content="first line\nblablabla\n")
        self.assertTrue(cr.evaluate({"triggering_file_content_regex": r".+bla"}, ctx))
        self.assertFalse(cr.evaluate({"triggering_file_content_regex": r"WONT_MATCH"}, ctx))

    def test_content_regex_missing_file_returns_false(self) -> None:
        ctx = cr.EvalContext(
            file_path_abs=self.repo.project_root / "nope.py",
            file_path_rel="nope.py",
            project_root=self.repo.project_root,
        )
        self.assertFalse(cr.evaluate({"triggering_file_content_regex": r".+"}, ctx))

    def test_content_regex_returns_false_when_file_outside_project(self) -> None:
        # Regression: triggering_file_content_regex used to read file_path_abs
        # without checking file_path_rel, so a tool acting on a file *outside*
        # the project would still match content. All triggering_file_*
        # predicates must agree: a file outside CLAUDE_PROJECT_DIR never matches.
        with tempfile.TemporaryDirectory() as other:
            external = Path(other) / "outsider.py"
            external.write_text("matches the pattern\n")
            ctx = cr.EvalContext(
                file_path_abs=external.resolve(),
                file_path_rel=None,
                project_root=self.repo.project_root,
            )
            self.assertFalse(cr.evaluate({"triggering_file_content_regex": r"matches"}, ctx))

    def test_any_file_exists_exact_path(self) -> None:
        self.repo.write_file("tests/__init__.py")
        ctx = self._ctx("src/a.py")
        self.assertTrue(cr.evaluate({"any_file_exists": "tests"}, ctx))
        self.assertFalse(cr.evaluate({"any_file_exists": "tests_missing"}, ctx))

    def test_any_file_exists_with_glob_pattern(self) -> None:
        self.repo.write_file("pyproject.toml")
        ctx = self._ctx("src/a.py")
        self.assertTrue(cr.evaluate({"any_file_exists": "*.toml"}, ctx))
        self.assertFalse(cr.evaluate({"any_file_exists": "*.yaml"}, ctx))

    def test_any_file_exists_with_recursive_glob(self) -> None:
        self.repo.write_file("src/module.cs")
        ctx = self._ctx("src/a.py")
        self.assertTrue(cr.evaluate({"any_file_exists": "**/*.cs"}, ctx))
        self.assertFalse(cr.evaluate({"any_file_exists": "**/*.rb"}, ctx))

    def test_any_file_exists_dot_matches_project_root(self) -> None:
        # "." is the spec's always-true sentinel for SessionStart rules.
        ctx = cr.EvalContext(
            file_path_abs=None,
            file_path_rel=None,
            project_root=self.repo.project_root,
        )
        self.assertTrue(cr.evaluate({"any_file_exists": "."}, ctx))

    def test_all_of_short_circuits_before_content_read(self) -> None:
        ctx = self._ctx("src/other.py", content="has bla")
        with mock.patch.object(ctx, "read_content") as read_mock:
            cond = {
                "all_of": [
                    {"triggering_file_path_glob": "src/acme.py"},
                    {"triggering_file_content_regex": r"bla"},
                ]
            }
            self.assertFalse(cr.evaluate(cond, ctx))
            read_mock.assert_not_called()

    def test_any_of_short_circuits(self) -> None:
        ctx = self._ctx("src/acme.py")
        with mock.patch.object(ctx, "read_content") as read_mock:
            cond = {
                "any_of": [
                    {"triggering_file_path_glob": "src/acme.py"},
                    {"triggering_file_content_regex": r"bla"},
                ]
            }
            self.assertTrue(cr.evaluate(cond, ctx))
            read_mock.assert_not_called()

    def test_any_of_returns_false_when_all_children_false(self) -> None:
        ctx = self._ctx("src/a.py")
        result = cr.evaluate(
            {
                "any_of": [
                    {"triggering_file_path_glob": "nope/*.py"},
                    {"triggering_file_path_glob": "also_nope/*.py"},
                ]
            },
            ctx,
        )
        self.assertFalse(result)

    def test_not_inverts(self) -> None:
        ctx = self._ctx("src/a.py")
        self.assertTrue(cr.evaluate({"not": {"triggering_file_path_glob": "nope/*.py"}}, ctx))
        self.assertFalse(cr.evaluate({"not": {"triggering_file_path_glob": "src/*.py"}}, ctx))

    def test_mixed_and_or_not(self) -> None:
        ctx = self._ctx("src/core/thing.py")
        cond = {
            "all_of": [
                {
                    "any_of": [
                        {"triggering_file_path_glob": "src/core/**/*.py"},
                        {"triggering_file_path_glob": "src/domain/**/*.py"},
                    ]
                },
                {"not": {"triggering_file_path_glob": "**/generated/**"}},
            ]
        }
        self.assertTrue(cr.evaluate(cond, ctx))

        ctx2 = self._ctx("src/core/generated/x.py")
        self.assertFalse(cr.evaluate(cond, ctx2))

    def test_content_is_cached_across_predicates(self) -> None:
        ctx = self._ctx("src/acme.py", content="has bla")
        with mock.patch(
            "conditional_rules._read_file_capped",
            wraps=cr._read_file_capped,
        ) as read_mock:
            cond = {
                "all_of": [
                    {"triggering_file_content_regex": r"has"},
                    {"triggering_file_content_regex": r"bla"},
                ]
            }
            self.assertTrue(cr.evaluate(cond, ctx))
            self.assertEqual(read_mock.call_count, 1)

    def test_any_file_content_regex_runtime_match(self) -> None:
        self.repo.write_project_file("pyproject.toml", "[project]\nrequires-python = '>=3.13'\n")
        ctx = cr.EvalContext(
            file_path_abs=None,
            file_path_rel=None,
            project_root=self.repo.project_root,
        )
        self.assertTrue(
            cr.evaluate(
                {
                    "any_file_content_regex": {
                        "path": "pyproject.toml",
                        "pattern": r"requires-python.*3\.\d+",
                    },
                },
                ctx,
            )
        )
        self.assertFalse(
            cr.evaluate(
                {
                    "any_file_content_regex": {
                        "path": "pyproject.toml",
                        "pattern": "WILL_NOT_MATCH",
                    },
                },
                ctx,
            )
        )

    def test_any_file_content_regex_missing_file_returns_false(self) -> None:
        ctx = cr.EvalContext(
            file_path_abs=None,
            file_path_rel=None,
            project_root=self.repo.project_root,
        )
        self.assertFalse(
            cr.evaluate(
                {"any_file_content_regex": {"path": "nope.toml", "pattern": ".+"}},
                ctx,
            )
        )

    def test_any_file_content_regex_cache_reads_each_path_once(self) -> None:
        self.repo.write_project_file("pyproject.toml", "content here\n")
        ctx = cr.EvalContext(
            file_path_abs=None,
            file_path_rel=None,
            project_root=self.repo.project_root,
        )
        with mock.patch(
            "conditional_rules._read_file_capped",
            wraps=cr._read_file_capped,
        ) as read_mock:
            cond = {
                "all_of": [
                    {
                        "any_file_content_regex": {
                            "path": "pyproject.toml",
                            "pattern": "content",
                        }
                    },
                    {
                        "any_file_content_regex": {
                            "path": "pyproject.toml",
                            "pattern": "here",
                        }
                    },
                ]
            }
            self.assertTrue(cr.evaluate(cond, ctx))
            self.assertEqual(read_mock.call_count, 1)


# ---------- Path normalization ----------


class PathNormalizationTests(_RepoTestCase):
    def test_inside_project_returns_posix_rel(self) -> None:
        p = self.repo.write_file("src/a.py")
        rel = cr.normalize_project_relative(str(p), self.repo.project_root)
        self.assertEqual(rel, "src/a.py")

    def test_outside_project_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            p = Path(other) / "a.py"
            p.write_text("")
            rel = cr.normalize_project_relative(str(p), self.repo.project_root)
            self.assertIsNone(rel)

    def test_none_input_returns_none(self) -> None:
        self.assertIsNone(cr.normalize_project_relative(None, self.repo.project_root))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(cr.normalize_project_relative("", self.repo.project_root))

    def test_nul_byte_in_file_path_returns_none(self) -> None:
        """An embedded NUL makes `Path.resolve()` raise `ValueError`, not
        `OSError` (Python 3.14). Both are swallowed: the caller gets "no
        triggering file" instead of an exception that would kill rule injection
        for the whole event."""
        rel = cr.normalize_project_relative(
            f"{self.repo.project_root}/src/a\x00b.py",
            self.repo.project_root,
        )
        self.assertIsNone(rel)

    def test_nul_byte_in_project_root_returns_none(self) -> None:
        """The same guard covers the `project_root.resolve()` side."""
        rel = cr.normalize_project_relative(
            str(self.repo.write_file("src/a.py")),
            Path(f"{self.repo.project_root}\x00x"),
        )
        self.assertIsNone(rel)


# ---------- Output shape builders ----------


class OutputShapeTests(unittest.TestCase):
    def test_block_output_shape(self) -> None:
        out = cr.build_block_output("bad thing at rules[0]")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("bad thing", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_success_output_shape(self) -> None:
        out = cr.build_output(
            [
                ("r1", "body one", "rules.json#r1"),
                ("r2", "body two", "r2.md"),
            ],
            "PreToolUse",
        )
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## Rule: r1\nbody one", ac)
        self.assertIn("## Rule: r2\nbody two", ac)

    def test_additional_context_opens_with_manifest(self) -> None:
        out = cr.build_output(
            [
                ("r1", "body one", "rules.json#r1"),
                ("r2", "body two", "r2.md"),
            ],
            "PreToolUse",
        )
        ac = out["hookSpecificOutput"]["additionalContext"]
        first_line = ac.split("\n", 1)[0]
        self.assertTrue(first_line.startswith("<!-- Conditional rules active (2): r1, r2."))
        self.assertTrue(first_line.endswith("-->"))
        self.assertIn("Read that file in full now", first_line)
        self.assertLess(ac.index("<!-- Conditional rules active"), ac.index("## Rule: r1"))

    def test_system_message_lists_each_rule_source(self) -> None:
        out = cr.build_output(
            [
                ("r1", "b1", "rules.json#r1"),
                ("r2", "b2", "r2.md"),
            ],
            "PreToolUse",
        )
        self.assertEqual(
            out["systemMessage"],
            "Conditional Rules: Loaded rules.json#r1\nConditional Rules: Loaded r2.md",
        )

    def test_sessionstart_output_has_correct_event_name(self) -> None:
        out = cr.build_output([("r1", "b", "rules.json#r1")], "SessionStart")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(cr.build_output([], "PreToolUse"))


if __name__ == "__main__":
    unittest.main()
