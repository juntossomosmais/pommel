#!/usr/bin/env python3
"""
Tests for the interaction between marketplace rules and project-level rules.

Covers the semantics that emerge only when both sources are active simultaneously:
  - Coexistence: both sources inject into the same additionalContext
  - activation_criteria False on marketplace while project rules are present
  - Source ordering: marketplace content appears before project content
  - PreToolUse with both sources active
  - Cross-source rule-ID collision: second source is deduped within the session
  - Config error in one source while the other source is healthy

Isolated marketplace tests live in test_conditional_rules_for_marketplace.py.
Isolated project tests live in test_conditional_rules_for_project.py.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from _test_helpers import (
    _make_e2e_input,
    _RepoTestCase,
    _rule,
    cr,
    create_structure,
)


class ProjectAndMarketplaceTests(_RepoTestCase):
    def _write_project_rules(self, data: dict) -> None:
        project_rules_dir = self.repo.project_root / ".claude" / "conditional_rules"
        project_rules_dir.mkdir(parents=True, exist_ok=True)
        (project_rules_dir / "rules.json").write_text(json.dumps(data), encoding="utf-8")

    def test_project_rules_coexist_with_marketplace_rules(self) -> None:
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="marketplace-rule",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="marketplace content",
                    )
                ],
            }
        )
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        id="project-rule",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="project content",
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("marketplace content", ac)
        self.assertIn("project content", ac)

    def test_source_ordering_marketplace_content_before_project_content(self) -> None:
        # Marketplace sources are collected before the project source, so their
        # output must appear first in additionalContext.
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="mp-rule",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="MARKETPLACE",
                    )
                ],
            }
        )
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        id="proj-rule",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="PROJECT",
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertLess(ac.index("MARKETPLACE"), ac.index("PROJECT"))

    def test_pretooluse_both_sources_inject(self) -> None:
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="mp-pre",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="marketplace pre",
                    )
                ],
            }
        )
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        id="proj-pre",
                        when={"triggering_file_path_glob": "src/*.py"},
                        content="project pre",
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel="src/a.py")
        out = cr.handle("PreToolUse", inp, self.repo.paths)
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("marketplace pre", ac)
        self.assertIn("project pre", ac)

    def test_marketplace_activation_criteria_false_project_rules_still_fire(self) -> None:
        # Marketplace plugin requires *.csproj (absent); project rule must still fire.
        self.repo.write_rules(
            {
                "activation_criteria": {"any_file_exists": "*.csproj"},
                "rules": [
                    _rule(
                        id="mp-gated",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="marketplace gated",
                    )
                ],
            }
        )
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        id="proj-always",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="project always",
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNotNone(out)
        ac = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("project always", ac)
        self.assertNotIn("marketplace gated", ac)

    def test_cross_source_id_collision_first_to_fire_wins_session_slot(self) -> None:
        # If marketplace and project rules share the same rule ID, the first source
        # to fire (marketplace) claims the session cache slot; the project rule is
        # treated as already-injected and skipped for the rest of the session.
        self.repo.write_file("src/a.py")
        self.repo.write_rules(
            {
                "rules": [
                    _rule(
                        id="shared-id",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="from marketplace",
                    )
                ],
            }
        )
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        id="shared-id",
                        fires_on_matcher="*",
                        when={"any_file_exists": "src"},
                        content="from project",
                    )
                ],
            }
        )
        session_id = "sess-collision"
        inp_session = self.repo.make_input(
            file_rel=None, event="SessionStart", session_id=session_id
        )
        out_session = cr.handle("SessionStart", inp_session, self.repo.paths)
        # Marketplace fires first → its content is injected.
        self.assertIsNotNone(out_session)
        self.assertIn("from marketplace", out_session["hookSpecificOutput"]["additionalContext"])

        # On subsequent PreToolUse the project rule with the same id is deduped.
        inp_pre = self.repo.make_input(file_rel="src/a.py", session_id=session_id)
        out_pre = cr.handle("PreToolUse", inp_pre, self.repo.paths)
        self.assertIsNone(out_pre)

    def test_config_error_in_marketplace_does_not_block_healthy_project_rules(self) -> None:
        # Marketplace has a broken rules.json; project rules are valid and must inject.
        self.repo.write_rules_raw("not json at all")
        self._write_project_rules(
            {
                "rules": [
                    _rule(
                        id="proj-healthy",
                        fires_on_matcher="SessionStart",
                        when={"any_file_exists": "src"},
                        content="project healthy",
                    )
                ],
            }
        )
        inp = self.repo.make_input(file_rel=None, event="SessionStart")
        with mock.patch("sys.stderr"):
            out = cr.handle("SessionStart", inp, self.repo.paths)
        self.assertIsNotNone(out)
        self.assertIn("project healthy", out["hookSpecificOutput"]["additionalContext"])


class ProjectOverridesActivationCriteriaEndToEndTests(unittest.TestCase):
    """Project-level rules.json ignores activation_criteria."""

    def test_project_rules_override_activation_criteria(self) -> None:
        structure = {
            "plugin": {},
            "marketplace": {},
            "project": {
                "src": {"a.py": "pass\n"},
                ".claude": {
                    "conditional_rules": {
                        "rules.json": json.dumps(
                            {
                                "activation_criteria": {"any_file_exists": "*.csproj"},
                                "rules": [
                                    _rule(
                                        id="proj-rule",
                                        fires_on_matcher="SessionStart",
                                        when={"any_file_exists": "src"},
                                        content="project body",
                                    )
                                ],
                            }
                        ),
                    }
                },
            },
        }
        with create_structure(structure) as (root, _created):
            plugin_root = root / "plugin"
            project_root = root / "project"
            marketplace_dir = root / "marketplace"
            paths = cr.Paths.for_test(plugin_root, project_root, marketplace_dir=marketplace_dir)

            out = cr.handle(
                "SessionStart",
                _make_e2e_input(project_root, None, event="SessionStart"),
                paths,
            )
            self.assertIsNotNone(out)
            self.assertIn("project body", out["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
