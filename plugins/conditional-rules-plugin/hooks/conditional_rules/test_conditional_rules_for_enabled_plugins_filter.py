#!/usr/bin/env python3
"""
Tests for the `enabledPlugins` settings filter applied to registry-based discovery.

The installed-plugins registry (`installed_plugins.json`) only records what
is *installed*. Whether a plugin actually contributes its rules depends on
`enabledPlugins` in the user/project settings — that's where `/plugin disable`
flips a key to `false`. These tests verify that the hook respects that gate
across precedence, defaults, and malformed inputs.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _test_helpers import (
    _EntryScriptRunner,
    _RepoFixture,
    _RepoTestCase,
    _rule,
    cr,
)


def _registry_with(*records: tuple[str, dict]) -> dict:
    plugins: dict[str, list[dict]] = {}
    for plugin_key, record in records:
        plugins.setdefault(plugin_key, []).append(record)
    return {"version": 2, "plugins": plugins}


def _record(install_path: Path, *, scope: str = "user", version: str = "0.1.0") -> dict:
    """Build one registry scope record.

    `scope` stays `user` throughout this file: these tests are about the
    `enabledPlugins` gate, so every record must clear the install-scope gate
    (`_record_applies_to_project`) without needing a `projectPath`.
    """
    return {
        "scope": scope,
        "installPath": str(install_path),
        "version": version,
    }


# ---------- _read_enabled_plugins_section ----------


class ReadEnabledPluginsSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RepoFixture()
        self.addCleanup(self.repo.cleanup)

    def test_missing_file_returns_empty(self) -> None:
        ghost = self.repo.user_settings_file.parent / "missing.json"
        self.assertEqual(cr._read_enabled_plugins_section(ghost), {})

    def test_empty_file_returns_empty(self) -> None:
        self.repo.write_user_settings_raw("")
        self.assertEqual(cr._read_enabled_plugins_section(self.repo.user_settings_file), {})

    def test_invalid_json_returns_empty(self) -> None:
        self.repo.write_user_settings_raw("{not valid json")
        self.assertEqual(cr._read_enabled_plugins_section(self.repo.user_settings_file), {})

    def test_non_object_top_level_returns_empty(self) -> None:
        self.repo.write_user_settings_raw('["arrays", "not", "objects"]')
        self.assertEqual(cr._read_enabled_plugins_section(self.repo.user_settings_file), {})

    def test_missing_enabled_plugins_returns_empty(self) -> None:
        self.repo.write_user_settings({"otherKey": True})
        self.assertEqual(cr._read_enabled_plugins_section(self.repo.user_settings_file), {})

    def test_non_object_enabled_plugins_returns_empty(self) -> None:
        self.repo.write_user_settings({"enabledPlugins": ["a", "b"]})
        self.assertEqual(cr._read_enabled_plugins_section(self.repo.user_settings_file), {})

    def test_non_bool_values_are_dropped(self) -> None:
        self.repo.write_user_settings(
            {
                "enabledPlugins": {
                    "good@mp": True,
                    "bad-string@mp": "true",
                    "bad-int@mp": 1,
                    "bad-null@mp": None,
                    "off@mp": False,
                }
            }
        )
        self.assertEqual(
            cr._read_enabled_plugins_section(self.repo.user_settings_file),
            {"good@mp": True, "off@mp": False},
        )


# ---------- _load_disabled_plugins precedence ----------


class LoadDisabledPluginsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RepoFixture()
        self.addCleanup(self.repo.cleanup)

    def _disabled(self) -> set[str]:
        return cr._load_disabled_plugins(self.repo.user_settings_file, self.repo.project_root)

    def test_no_files_returns_empty(self) -> None:
        self.assertEqual(self._disabled(), set())

    def test_explicit_false_at_user_scope(self) -> None:
        self.repo.write_user_settings({"enabledPlugins": {"x@mp": False}})
        self.assertEqual(self._disabled(), {"x@mp"})

    def test_explicit_false_at_project_scope(self) -> None:
        self.repo.write_project_settings({"enabledPlugins": {"x@mp": False}})
        self.assertEqual(self._disabled(), {"x@mp"})

    def test_explicit_false_at_project_local_scope(self) -> None:
        self.repo.write_project_local_settings({"enabledPlugins": {"x@mp": False}})
        self.assertEqual(self._disabled(), {"x@mp"})

    def test_project_overrides_user_to_enable(self) -> None:
        self.repo.write_user_settings({"enabledPlugins": {"x@mp": False}})
        self.repo.write_project_settings({"enabledPlugins": {"x@mp": True}})
        self.assertEqual(self._disabled(), set())

    def test_project_overrides_user_to_disable(self) -> None:
        self.repo.write_user_settings({"enabledPlugins": {"x@mp": True}})
        self.repo.write_project_settings({"enabledPlugins": {"x@mp": False}})
        self.assertEqual(self._disabled(), {"x@mp"})

    def test_local_overrides_project_to_enable(self) -> None:
        self.repo.write_project_settings({"enabledPlugins": {"x@mp": False}})
        self.repo.write_project_local_settings({"enabledPlugins": {"x@mp": True}})
        self.assertEqual(self._disabled(), set())

    def test_local_overrides_project_to_disable(self) -> None:
        self.repo.write_project_settings({"enabledPlugins": {"x@mp": True}})
        self.repo.write_project_local_settings({"enabledPlugins": {"x@mp": False}})
        self.assertEqual(self._disabled(), {"x@mp"})

    def test_independent_keys_merge_across_scopes(self) -> None:
        """Each plugin key is decided independently from the others."""
        self.repo.write_user_settings({"enabledPlugins": {"u@mp": True}})
        self.repo.write_project_settings({"enabledPlugins": {"p@mp": False}})
        self.repo.write_project_local_settings({"enabledPlugins": {"l@mp": False, "u@mp": False}})
        self.assertEqual(self._disabled(), {"p@mp", "l@mp", "u@mp"})

    def test_absent_key_means_enabled(self) -> None:
        """Plugins not mentioned anywhere default to enabled."""
        self.repo.write_user_settings({"enabledPlugins": {"other@mp": True}})
        self.assertEqual(self._disabled(), set())

    def test_malformed_files_are_tolerated(self) -> None:
        self.repo.write_user_settings_raw("{not valid")
        self.assertEqual(self._disabled(), set())


# ---------- _discover_from_registry param contract ----------


class DiscoverFromRegistryDisabledFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RepoFixture()
        self.addCleanup(self.repo.cleanup)

    def _two_plugins(self) -> None:
        a = self.repo.make_install_dir("mp", "alpha", "1.0.0")
        b = self.repo.make_install_dir("mp", "bravo", "1.0.0")
        (a / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="alpha", content="a")]}),
            encoding="utf-8",
        )
        (b / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="bravo", content="b")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            _registry_with(
                ("alpha@mp", _record(a)),
                ("bravo@mp", _record(b)),
            )
        )

    def test_default_none_filters_nothing(self) -> None:
        self._two_plugins()
        sources = cr._discover_from_registry(
            self.repo.installed_plugins_file, self.repo.project_root
        )
        names = sorted(s.rules_file.parent.parent.name for s in sources)
        self.assertEqual(names, ["alpha", "bravo"])

    def test_empty_set_filters_nothing(self) -> None:
        self._two_plugins()
        sources = cr._discover_from_registry(
            self.repo.installed_plugins_file, self.repo.project_root, set()
        )
        self.assertEqual(len(sources), 2)

    def test_disabled_key_skipped(self) -> None:
        self._two_plugins()
        sources = cr._discover_from_registry(
            self.repo.installed_plugins_file, self.repo.project_root, {"alpha@mp"}
        )
        self.assertEqual(len(sources), 1)
        self.assertIn("bravo", str(sources[0].rules_file))

    def test_disabled_key_not_in_registry_is_a_noop(self) -> None:
        self._two_plugins()
        sources = cr._discover_from_registry(
            self.repo.installed_plugins_file, self.repo.project_root, {"ghost@mp"}
        )
        self.assertEqual(len(sources), 2)


# ---------- End-to-end via cr.handle ----------


class EndToEndDisabledFilterTests(_RepoTestCase):
    def _install(self, plugin: str, body: str) -> Path:
        install = self.repo.make_install_dir("mp", plugin, "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id=plugin, content=body)]}),
            encoding="utf-8",
        )
        return install

    def test_plugin_disabled_at_project_scope_is_skipped(self) -> None:
        """Reproduces the bug: backend-csharp-plugin disabled in project
        settings.json must not contribute its rules."""
        a = self._install("alpha", "alpha body")
        b = self._install("bravo", "bravo body")
        self.repo.write_installed_plugins(
            _registry_with(
                ("alpha@mp", _record(a)),
                ("bravo@mp", _record(b)),
            )
        )
        self.repo.write_project_settings({"enabledPlugins": {"alpha@mp": False, "bravo@mp": True}})
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("alpha body", ctx)
        self.assertIn("bravo body", ctx)

    def test_plugin_disabled_at_user_scope_is_skipped(self) -> None:
        a = self._install("alpha", "alpha body")
        self.repo.write_installed_plugins(_registry_with(("alpha@mp", _record(a))))
        self.repo.write_user_settings({"enabledPlugins": {"alpha@mp": False}})
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)

    def test_plugin_disabled_at_project_local_scope_is_skipped(self) -> None:
        a = self._install("alpha", "alpha body")
        self.repo.write_installed_plugins(_registry_with(("alpha@mp", _record(a))))
        self.repo.write_project_local_settings({"enabledPlugins": {"alpha@mp": False}})
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)

    def test_project_scope_override_re_enables_plugin(self) -> None:
        """User disables a plugin globally but the project re-enables it."""
        a = self._install("alpha", "alpha body")
        self.repo.write_installed_plugins(_registry_with(("alpha@mp", _record(a))))
        self.repo.write_user_settings({"enabledPlugins": {"alpha@mp": False}})
        self.repo.write_project_settings({"enabledPlugins": {"alpha@mp": True}})
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("alpha body", out["hookSpecificOutput"]["additionalContext"])

    def test_local_scope_override_disables_plugin_enabled_at_project(self) -> None:
        a = self._install("alpha", "alpha body")
        self.repo.write_installed_plugins(_registry_with(("alpha@mp", _record(a))))
        self.repo.write_project_settings({"enabledPlugins": {"alpha@mp": True}})
        self.repo.write_project_local_settings({"enabledPlugins": {"alpha@mp": False}})
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)

    def test_no_settings_files_loads_everything_default(self) -> None:
        """Regression: no settings file at all → previous behavior preserved."""
        a = self._install("alpha", "alpha body")
        self.repo.write_installed_plugins(_registry_with(("alpha@mp", _record(a))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("alpha body", out["hookSpecificOutput"]["additionalContext"])

    def test_malformed_settings_does_not_crash_or_block(self) -> None:
        """A broken settings.json must not stop tool calls — we silently
        treat it as 'no overrides' and load every installed plugin."""
        a = self._install("alpha", "alpha body")
        self.repo.write_installed_plugins(_registry_with(("alpha@mp", _record(a))))
        self.repo.write_user_settings_raw("{not valid json")
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("alpha body", out["hookSpecificOutput"]["additionalContext"])

    def test_project_rules_unaffected_by_enabled_plugins(self) -> None:
        """`enabledPlugins` gates marketplace plugins. The project-level
        `.claude/conditional_rules/rules.json` is always applied."""
        a = self._install("alpha", "marketplace body")
        self.repo.write_installed_plugins(_registry_with(("alpha@mp", _record(a))))
        self.repo.write_project_settings({"enabledPlugins": {"alpha@mp": False}})
        proj_rules = self.repo.project_root / ".claude" / "conditional_rules"
        proj_rules.mkdir(parents=True, exist_ok=True)
        (proj_rules / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="proj", content="project body")]}),
            encoding="utf-8",
        )
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("project body", ctx)
        self.assertNotIn("marketplace body", ctx)


# ---------- Entry script picks up the env var ----------


class EntryScriptUsesUserSettingsFileEnvVarTests(_RepoTestCase):
    def test_pre_script_skips_disabled_plugin_via_env(self) -> None:
        install = self.repo.make_install_dir("mp", "off", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="off", content="should not load")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("off@mp", _record(install))))
        self.repo.write_user_settings({"enabledPlugins": {"off@mp": False}})
        self.repo.write_file("src/a.py")

        runner = _EntryScriptRunner(
            "conditional_rules_pre.py",
            self.repo.plugin_root,
            self.repo.project_root,
            self.repo.marketplace_dir,
            installed_plugins_file=self.repo.installed_plugins_file,
            user_settings_file=self.repo.user_settings_file,
        )
        rc, out, err = runner.run(
            json.dumps(self.repo.make_input(file_rel="src/a.py", session_id="env-sess"))
        )
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
