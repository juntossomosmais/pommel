#!/usr/bin/env python3
"""
Tests for registry-based marketplace plugin discovery.

When `installed_plugins.json` exists, the hook reads each scope record's
`installPath` and looks for `rules.json` directly under that directory.
This is the production discovery path: it covers the user/project/local/managed
install scopes and ignores stale older versions still on disk under the
cache root. Each record is first gated by its own `scope` (see
`RegistryInstallScopeGatingTests`) — `project`/`local` records only apply to
the repo named by their `projectPath`.

When the registry is absent, the hook falls back to a recursive scan of
`marketplace_dir`. Existing tests cover that path; this file covers the
registry path and the precedence between the two.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from _test_helpers import (
    _EntryScriptRunner,
    _RepoFixture,
    _RepoTestCase,
    _rule,
    cr,
)


def _registry_with(*records: tuple[str, dict]) -> dict:
    """Build a registry-shaped dict from `(plugin_key, record)` pairs.

    Multiple records under the same plugin_key are merged into the same
    list (mirroring the multi-scope shape Claude Code writes).
    """
    plugins: dict[str, list[dict]] = {}
    for plugin_key, record in records:
        plugins.setdefault(plugin_key, []).append(record)
    return {"version": 2, "plugins": plugins}


def _record(
    install_path: Path,
    *,
    scope: str = "user",
    version: str = "0.1.0",
    project_path: Path | str | None = None,
) -> dict:
    """Build one registry scope record.

    `scope` defaults to `user`, which applies to every project, so fixtures
    that don't care about install scope stay eligible everywhere. Pass
    `project_path` to add the `projectPath` key Claude Code writes on
    `project`/`local` records — those only apply when it resolves to the
    project the hook is running in.
    """
    record: dict = {
        "scope": scope,
        "installPath": str(install_path),
        "version": version,
    }
    if project_path is not None:
        record["projectPath"] = str(project_path)
    return record


def _session_start_input(project_root: Path, session_id: str) -> dict:
    """Minimal SessionStart payload — no `tool_name`, no `tool_input`.

    Takes an explicit `project_root` so a single test can fire the same
    event in two different repos.
    """
    return {
        "session_id": session_id,
        "hook_event_name": "SessionStart",
        "cwd": str(project_root),
    }


# ---------- Basic discovery ----------


class RegistryBasicDiscoveryTests(_RepoTestCase):
    def test_registry_with_one_plugin_yields_rules(self) -> None:
        install = self.repo.make_install_dir("mp", "plug-a", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        _rule(id="from-registry", content="hello from registry"),
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("plug-a@mp", _record(install))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("hello from registry", out["hookSpecificOutput"]["additionalContext"])

    def test_multiple_plugins_each_contribute_sources(self) -> None:
        install_a = self.repo.make_install_dir("mp", "plug-a", "1.0.0")
        install_b = self.repo.make_install_dir("mp", "plug-b", "2.3.4")
        (install_a / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="rule-a", content="A body")]}),
            encoding="utf-8",
        )
        (install_b / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="rule-b", content="B body")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            _registry_with(
                ("plug-a@mp", _record(install_a)),
                ("plug-b@mp", _record(install_b)),
            )
        )
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("A body", ctx)
        self.assertIn("B body", ctx)

    def test_install_path_pointing_at_nonexistent_dir_is_skipped(self) -> None:
        ghost = Path(self.repo.installed_plugins_file).parent / "ghost-plugin"
        # Don't create `ghost`. Discovery should silently skip.
        good = self.repo.make_install_dir("mp", "real", "1.0.0")
        (good / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="real", content="present")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            _registry_with(
                ("ghost@mp", _record(ghost)),
                ("real@mp", _record(good)),
            )
        )
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("present", out["hookSpecificOutput"]["additionalContext"])

    def test_install_path_without_rules_json_is_skipped(self) -> None:
        no_rules = self.repo.make_install_dir("mp", "no-rules", "1.0.0")
        # Create the dir but no rules.json.
        with_rules = self.repo.make_install_dir("mp", "with-rules", "1.0.0")
        (with_rules / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="found", content="found me")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            _registry_with(
                ("no-rules@mp", _record(no_rules)),
                ("with-rules@mp", _record(with_rules)),
            )
        )
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("found me", out["hookSpecificOutput"]["additionalContext"])

    def test_dedup_by_install_path_when_plugin_appears_at_multiple_scopes(self) -> None:
        """A plugin installed at user AND project scope shares one installPath.

        The registry stores multiple records (one per scope) but they all
        point at the same versioned cache directory. We must inject the
        rule exactly once.

        The project-scope record carries the current project as its
        `projectPath` so it passes the scope gate — otherwise it would be
        dropped before dedup ever ran and this test would prove nothing.
        """
        install = self.repo.make_install_dir("mp", "shared", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="dedup", content="once only")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            _registry_with(
                ("shared@mp", _record(install, scope="user")),
                (
                    "shared@mp",
                    _record(
                        install,
                        scope="project",
                        project_path=self.repo.project_root,
                    ),
                ),
            )
        )
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        # Each rule appears under one "## Rule:" header. Count occurrences.
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx.count("## Rule: dedup"), 1)


# ---------- Install-scope gating (`scope` + `projectPath`) ----------


class RegistryInstallScopeGatingTests(_RepoTestCase):
    """A record's `scope` decides whether its `installPath` is looked at at all.

    Every registry record describes where a plugin is installed *for one
    scope*. `user` and `managed` records install it for every project;
    `project` and `local` records install it for the single repo named by
    their `projectPath`. Before this gate existed, discovery walked every
    record's `installPath` unconditionally, so a plugin installed at project
    scope for repo A injected its rules into every other repo the user
    opened.

    The gate fails closed: any record we cannot positively place (missing,
    non-string, or unrecognized `scope`; a `project`/`local` record without a
    usable `projectPath`) contributes nothing. Skipping a source only means
    no rules are injected, while loading another repo's rules is the bug this
    gate exists to prevent.
    """

    CONTROL_BODY = "control plugin body"

    # ---- fixtures ----

    def _install_with_rule(self, plugin: str, body: str) -> Path:
        """Create a version-pinned install dir holding one valid rules.json."""
        install = self.repo.make_install_dir("mp", plugin, "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id=plugin, content=body)]}),
            encoding="utf-8",
        )
        return install

    def _control_plugin(self) -> tuple[str, dict]:
        """A user-scope plugin that must always load.

        Registered alongside a gated record so a test can tell "that record
        was skipped" apart from "discovery never ran at all".
        """
        install = self._install_with_rule("control", self.CONTROL_BODY)
        return ("control@mp", _record(install, scope="user"))

    def _sibling_project(self, name: str) -> Path:
        """Another repo under the same tmp root — 'the other project'."""
        other = self.repo.project_root.parent / name
        (other / "src").mkdir(parents=True, exist_ok=True)
        return other

    def _fire(self, session_id: str = "scope-sess") -> dict | None:
        """Run one PreToolUse `Edit` on `src/a.py` inside the fixture's project."""
        self.repo.write_file("src/a.py")
        return cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id=session_id),
            self.repo.paths,
        )

    def _fire_capturing_stderr(
        self,
        session_id: str = "scope-sess",
    ) -> tuple[dict | None, str]:
        """`_fire()` with stderr captured, returned as `(output, stderr_text)`.

        Discovery prints one diagnostic line per record whose `scope` it
        cannot place. Tests that trip that path assert the line here instead
        of leaking it into the test runner's output; tests that must stay
        silent assert the captured text is empty.
        """
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = self._fire(session_id=session_id)
        return out, buf.getvalue()

    def _assert_gated_but_control_loaded(
        self,
        out: dict | None,
        gated_body: str,
    ) -> None:
        """The control plugin loaded, the gated record contributed nothing."""
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.CONTROL_BODY, ctx)
        self.assertNotIn(gated_body, ctx)

    def _assert_one_unusable_scope_line(
        self,
        stderr_text: str,
        plugin_key: str,
        scope_repr: str,
    ) -> None:
        """Exactly one stderr line, naming the registry, the key, and the scope.

        `scope_repr` is the `{scope!r}` rendering the hook emits — `None` for a
        missing or null scope, `3` for a number, `'workspace'` for a string.
        """
        lines = [line for line in stderr_text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"expected exactly one stderr line, got {lines!r}")
        self.assertEqual(
            lines[0],
            f"conditional_rules: installed plugins registry "
            f"{self.repo.installed_plugins_file} has a record for {plugin_key} "
            f"with an unusable scope {scope_repr}; skipping it",
        )

    # ---- project / local scope: matching vs. foreign projectPath ----

    def test_project_scope_record_matching_project_root_loads_rules(self) -> None:
        """`projectPath` names the repo the hook runs in → the record applies."""
        install = self._install_with_rule("mine", "mine body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "mine@mp",
                    _record(
                        install,
                        scope="project",
                        project_path=self.repo.project_root,
                    ),
                )
            )
        )
        out = self._fire()
        assert out is not None
        self.assertIn("mine body", out["hookSpecificOutput"]["additionalContext"])

    def test_project_scope_record_installed_for_another_repo_does_not_leak(self) -> None:
        """THE regression: a plugin installed at project scope for repo A must
        not inject its rules while the hook is running in repo B.

        The install directory is perfectly valid and holds a valid rules.json;
        the only disqualifier is the record's `projectPath`.
        """
        install = self._install_with_rule("foreign", "foreign body")
        other_repo = self._sibling_project("other-repo")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "foreign@mp",
                    _record(install, scope="project", project_path=other_repo),
                )
            )
        )
        self.assertIsNone(self._fire())

    def test_local_scope_record_matching_project_root_loads_rules(self) -> None:
        """`local` is scoped exactly like `project` — same repo, so it applies."""
        install = self._install_with_rule("local-mine", "local mine body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "local-mine@mp",
                    _record(
                        install,
                        scope="local",
                        project_path=self.repo.project_root,
                    ),
                )
            )
        )
        out = self._fire()
        assert out is not None
        self.assertIn("local mine body", out["hookSpecificOutput"]["additionalContext"])

    def test_local_scope_record_installed_for_another_repo_does_not_leak(self) -> None:
        """A `local` record for someone else's repo is gated out too."""
        install = self._install_with_rule("local-foreign", "local foreign body")
        other_repo = self._sibling_project("other-repo")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "local-foreign@mp",
                    _record(install, scope="local", project_path=other_repo),
                )
            )
        )
        self.assertIsNone(self._fire())

    # ---- global scopes ignore projectPath entirely ----

    def test_managed_scope_record_loads_regardless_of_project_path(self) -> None:
        """`managed` installs apply everywhere; a stray `projectPath` naming
        another repo must not disqualify them."""
        install = self._install_with_rule("managed", "managed body")
        other_repo = self._sibling_project("other-repo")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "managed@mp",
                    _record(install, scope="managed", project_path=other_repo),
                )
            )
        )
        out = self._fire()
        assert out is not None
        self.assertIn("managed body", out["hookSpecificOutput"]["additionalContext"])

    def test_user_scope_record_loads_regardless_of_project_path(self) -> None:
        """Same for `user`: installed for every project, `projectPath` unread."""
        install = self._install_with_rule("user-wide", "user-wide body")
        other_repo = self._sibling_project("other-repo")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "user-wide@mp",
                    _record(install, scope="user", project_path=other_repo),
                )
            )
        )
        out = self._fire()
        assert out is not None
        self.assertIn("user-wide body", out["hookSpecificOutput"]["additionalContext"])

    # ---- project/local records with an unusable projectPath ----

    def test_project_scope_record_without_project_path_is_skipped(self) -> None:
        """No `projectPath` means we cannot place the record — fail closed."""
        install = self._install_with_rule("no-path", "no-path body")
        self.repo.write_installed_plugins(
            _registry_with(
                ("no-path@mp", _record(install, scope="project")),
                self._control_plugin(),
            )
        )
        self._assert_gated_but_control_loaded(self._fire(), "no-path body")

    def test_project_scope_record_with_empty_project_path_is_skipped(self) -> None:
        """An empty `projectPath` would otherwise resolve to the cwd."""
        install = self._install_with_rule("empty-path", "empty-path body")
        self.repo.write_installed_plugins(
            _registry_with(
                ("empty-path@mp", _record(install, scope="project", project_path="")),
                self._control_plugin(),
            )
        )
        self._assert_gated_but_control_loaded(self._fire(), "empty-path body")

    def test_project_scope_record_with_non_string_project_path_is_skipped(self) -> None:
        """A non-string `projectPath` is never fed to `Path()`."""
        install = self._install_with_rule("weird-path", "weird-path body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "weird-path@mp",
                    {
                        "scope": "project",
                        "projectPath": 42,
                        "installPath": str(install),
                        "version": "1.0.0",
                    },
                ),
                self._control_plugin(),
            )
        )
        self._assert_gated_but_control_loaded(self._fire(), "weird-path body")

    # ---- unusable `scope` values ----

    def test_record_without_scope_is_skipped(self) -> None:
        """With no `scope` we cannot tell whether the record applies here."""
        install = self._install_with_rule("scopeless", "scopeless body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "scopeless@mp",
                    {"installPath": str(install), "version": "1.0.0"},
                ),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "scopeless body")
        self._assert_one_unusable_scope_line(err, "scopeless@mp", "None")

    def test_record_with_null_scope_is_skipped(self) -> None:
        """`"scope": null` is not a string → skipped."""
        install = self._install_with_rule("null-scope", "null-scope body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "null-scope@mp",
                    {
                        "scope": None,
                        "installPath": str(install),
                        "version": "1.0.0",
                    },
                ),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "null-scope body")
        self._assert_one_unusable_scope_line(err, "null-scope@mp", "None")

    def test_record_with_numeric_scope_is_skipped(self) -> None:
        """`"scope": 3` is not a string → skipped."""
        install = self._install_with_rule("int-scope", "int-scope body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "int-scope@mp",
                    {
                        "scope": 3,
                        "installPath": str(install),
                        "version": "1.0.0",
                    },
                ),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "int-scope body")
        self._assert_one_unusable_scope_line(err, "int-scope@mp", "3")

    def test_record_with_unknown_scope_string_is_skipped(self) -> None:
        """A scope Claude Code may add later (here `workspace`) is unknown to
        this hook, so it is skipped rather than assumed global."""
        install = self._install_with_rule("workspace", "workspace body")
        self.repo.write_installed_plugins(
            _registry_with(
                ("workspace@mp", _record(install, scope="workspace")),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "workspace body")
        self._assert_one_unusable_scope_line(err, "workspace@mp", "'workspace'")

    def test_each_unusable_scope_record_gets_its_own_stderr_line(self) -> None:
        """The diagnostic is per record, not per registry: two unplaceable
        records in one invocation produce two lines, each naming its own key
        and scope value."""
        first = self._install_with_rule("weird", "weird body")
        second = self._install_with_rule("odd", "odd body")
        self.repo.write_installed_plugins(
            _registry_with(
                ("odd@mp", _record(second, scope="workspace")),
                ("weird@mp", {"installPath": str(first), "version": "1.0.0"}),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "weird body")
        self.assertNotIn("odd body", out["hookSpecificOutput"]["additionalContext"])
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, f"expected two stderr lines, got {lines!r}")
        self.assertIn("has a record for odd@mp with an unusable scope 'workspace'", err)
        self.assertIn("has a record for weird@mp with an unusable scope None", err)

    # ---- routine skips stay silent (they happen on every event) ----

    def test_foreign_project_record_skip_prints_nothing(self) -> None:
        """A project record belonging to another repo is the routine case: it is
        skipped without a word on stderr, because logging it would spam the
        diagnostic on every single hook event."""
        install = self._install_with_rule("foreign", "foreign body")
        other_repo = self._sibling_project("other-repo")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "foreign@mp",
                    _record(install, scope="project", project_path=other_repo),
                ),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "foreign body")
        self.assertEqual(err, "")

    def test_project_record_without_project_path_skip_prints_nothing(self) -> None:
        """Same for a project record we cannot place at all: the `scope` itself
        is recognized, so only the (silent) `projectPath` gate rejects it."""
        install = self._install_with_rule("no-path", "no-path body")
        self.repo.write_installed_plugins(
            _registry_with(
                ("no-path@mp", _record(install, scope="project")),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "no-path body")
        self.assertEqual(err, "")

    # ---- relative projectPath is rejected before it is resolved ----

    def test_project_scope_record_with_dot_project_path_is_skipped_from_project_cwd(
        self,
    ) -> None:
        """`projectPath: "."` must not match, even standing in the project root.

        Claude Code runs hooks with cwd set to the project dir, so a relative
        `projectPath` resolves to whatever repo the user happens to be in —
        i.e. it would pass the gate everywhere. This is the worst case: the
        process cwd genuinely IS the project root here, so only the
        `is_absolute()` rejection keeps the record out.
        """
        install = self._install_with_rule("dot-path", "dot-path body")
        self.repo.write_installed_plugins(
            _registry_with(
                ("dot-path@mp", _record(install, scope="project", project_path=".")),
                self._control_plugin(),
            )
        )
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo.project_root)
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "dot-path body")
        self.assertEqual(err, "")

    def test_project_scope_record_with_relative_project_path_is_skipped(self) -> None:
        """Any relative `projectPath`, not just `"."`, is rejected outright —
        Claude Code writes absolute paths, so nothing legitimate is lost."""
        install = self._install_with_rule("rel-path", "rel-path body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "rel-path@mp",
                    _record(install, scope="project", project_path="some/dir"),
                ),
                self._control_plugin(),
            )
        )
        self._assert_gated_but_control_loaded(self._fire(), "rel-path body")

    # ---- paths that cannot be resolved at all ----

    def test_project_scope_record_with_nul_in_project_path_is_skipped(self) -> None:
        """An embedded NUL makes `Path.resolve()` raise `ValueError` (not
        `OSError`) on Python 3.14, so the gate has to catch both.

        The path is absolute on purpose: a relative one would be rejected by
        the `is_absolute()` check before `resolve()` was ever reached, and this
        test would prove nothing about the exception guard.
        """
        install = self._install_with_rule("nul-path", "nul-path body")
        nul_project_path = f"{self.repo.project_root}/other\x00repo"
        self.assertTrue(Path(nul_project_path).is_absolute())
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "nul-path@mp",
                    _record(
                        install,
                        scope="project",
                        project_path=nul_project_path,
                    ),
                ),
                self._control_plugin(),
            )
        )
        out, err = self._fire_capturing_stderr()
        self._assert_gated_but_control_loaded(out, "nul-path body")
        self.assertEqual(err, "")

    # ---- path normalization when matching projectPath ----

    def test_project_path_with_trailing_slash_still_matches(self) -> None:
        """Comparison happens after `Path.resolve()`, so a cosmetic trailing
        separator is not a mismatch."""
        install = self._install_with_rule("slash", "slash body")
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "slash@mp",
                    _record(
                        install,
                        scope="project",
                        project_path=str(self.repo.project_root) + "/",
                    ),
                )
            )
        )
        out = self._fire()
        assert out is not None
        self.assertIn("slash body", out["hookSpecificOutput"]["additionalContext"])

    def test_project_path_reaching_project_root_via_symlink_matches(self) -> None:
        """A `projectPath` that resolves to the project root through a symlink
        is the same repo, so the record applies."""
        install = self._install_with_rule("linked", "linked body")
        link = self.repo.project_root.parent / "project-symlink"
        try:
            link.symlink_to(self.repo.project_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unsupported here: {exc}")
        self.repo.write_installed_plugins(
            _registry_with(("linked@mp", _record(install, scope="project", project_path=link)))
        )
        out = self._fire()
        assert out is not None
        self.assertIn("linked body", out["hookSpecificOutput"]["additionalContext"])

    # ---- the gate is the only reason a valid install is dropped ----

    def test_foreign_project_record_alone_contributes_nothing(self) -> None:
        """A plugin whose ONLY record is a foreign project record is silent —
        even though its `installPath` exists and carries a valid rules.json.

        Proven by flipping the very same record to `user` scope afterwards
        (fresh session so dedup doesn't mask it): the install then loads, so
        the scope gate was the only thing suppressing it.
        """
        install = self._install_with_rule("solo", "solo body")
        other_repo = self._sibling_project("other-repo")
        self.assertTrue((install / "rules.json").is_file())

        self.repo.write_installed_plugins(
            _registry_with(("solo@mp", _record(install, scope="project", project_path=other_repo)))
        )
        self.assertIsNone(self._fire(session_id="solo-gated"))

        self.repo.write_installed_plugins(
            _registry_with(("solo@mp", _record(install, scope="user", project_path=other_repo)))
        )
        out = self._fire(session_id="solo-ungated")
        assert out is not None
        self.assertIn("solo body", out["hookSpecificOutput"]["additionalContext"])

    def test_unresolvable_project_root_skips_project_records_but_keeps_user_ones(
        self,
    ) -> None:
        """When `project_root.resolve()` raises, no `projectPath` can be matched.

        Project/local records are all skipped (fail closed) while user/managed
        records still load — a broken project path must not silence globally
        installed plugins.
        """
        gated = self._install_with_rule("gated", "gated body")
        control_key, control_record = self._control_plugin()
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "gated@mp",
                    _record(
                        gated,
                        scope="project",
                        project_path=self.repo.project_root,
                    ),
                ),
                (control_key, control_record),
            )
        )

        target = self.repo.project_root
        real_resolve = Path.resolve

        def flaky_resolve(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self == target:
                raise OSError("simulated resolve failure")
            return real_resolve(self, *args, **kwargs)

        with mock.patch.object(Path, "resolve", flaky_resolve):
            sources = cr._discover_from_registry(
                self.repo.installed_plugins_file, self.repo.project_root
            )

        self.assertEqual(
            [str(s.rules_file) for s in sources],
            [str(Path(control_record["installPath"]) / "rules.json")],
        )

    def test_project_root_with_nul_skips_project_records_but_keeps_user_ones(
        self,
    ) -> None:
        """Same fail-closed split when the project root is unresolvable because
        of an embedded NUL, which raises `ValueError` rather than `OSError`.

        No mock here: the real `Path.resolve()` failure mode on Python 3.14.
        """
        gated = self._install_with_rule("gated", "gated body")
        control_key, control_record = self._control_plugin()
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "gated@mp",
                    _record(
                        gated,
                        scope="project",
                        project_path=self.repo.project_root,
                    ),
                ),
                (control_key, control_record),
            )
        )

        sources = cr._discover_from_registry(
            self.repo.installed_plugins_file,
            Path(f"{self.repo.project_root}\x00x"),
        )

        self.assertEqual(
            [str(s.rules_file) for s in sources],
            [str(Path(control_record["installPath"]) / "rules.json")],
        )

    # ---- the real incident, end to end ----

    def test_session_start_only_fires_in_the_project_the_plugin_belongs_to(self) -> None:
        """Mirrors the reported incident.

        A plugin is installed at project scope for project A, with an
        always-true rule that fires on every event. Starting a session in
        project B must inject nothing; the same SessionStart in project A
        injects the rule.
        """
        install = self.repo.make_install_dir("mp", "incident", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        _rule(
                            id="incident",
                            when={"any_file_exists": "."},
                            fires_on_matcher="*",
                            content="incident body",
                        )
                    ]
                }
            ),
            encoding="utf-8",
        )
        # Registry says: installed for project A (the fixture's project) only.
        self.repo.write_installed_plugins(
            _registry_with(
                (
                    "incident@mp",
                    _record(
                        install,
                        scope="project",
                        project_path=self.repo.project_root,
                    ),
                )
            )
        )

        project_b = self._sibling_project("project-b")
        paths_b = cr.Paths.for_test(
            self.repo.plugin_root,
            project_b,
            marketplace_dir=self.repo.marketplace_dir,
            installed_plugins_file=self.repo.installed_plugins_file,
            user_settings_file=self.repo.user_settings_file,
        )

        out_b = cr.handle(
            "SessionStart",
            _session_start_input(project_b, "incident-b"),
            paths_b,
        )
        self.assertIsNone(out_b)

        out_a = cr.handle(
            "SessionStart",
            _session_start_input(self.repo.project_root, "incident-a"),
            self.repo.paths,
        )
        assert out_a is not None
        self.assertIn("incident body", out_a["hookSpecificOutput"]["additionalContext"])


# ---------- Version drift / stale-cache safety ----------


class RegistryVersionDriftTests(_RepoTestCase):
    """The original motivation for moving to the registry."""

    def test_only_pinned_version_is_loaded_stale_sibling_ignored(self) -> None:
        """Both 0.1.0 and 0.2.0 are present on disk but the registry only
        points at 0.2.0 — only 0.2.0 rules should load."""
        old = self.repo.make_install_dir("mp", "plug", "0.1.0")
        new = self.repo.make_install_dir("mp", "plug", "0.2.0")
        (old / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="old", content="old version body")]}),
            encoding="utf-8",
        )
        (new / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="new", content="new version body")]}),
            encoding="utf-8",
        )
        # Registry pins to NEW version.
        self.repo.write_installed_plugins(
            _registry_with(("plug@mp", _record(new, version="0.2.0")))
        )
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new version body", ctx)
        self.assertNotIn("old version body", ctx)

    def test_simulated_upgrade_replaces_old_rules(self) -> None:
        """Simulate an upgrade by rewriting the registry mid-test."""
        old = self.repo.make_install_dir("mp", "plug", "0.1.0")
        new = self.repo.make_install_dir("mp", "plug", "0.2.0")
        (old / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="v1-only", content="v1 body")]}),
            encoding="utf-8",
        )
        (new / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="v2-only", content="v2 body")]}),
            encoding="utf-8",
        )
        self.repo.write_file("src/a.py")

        # First invocation: registry pinned to 0.1.0.
        self.repo.write_installed_plugins(
            _registry_with(("plug@mp", _record(old, version="0.1.0")))
        )
        out_v1 = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="s-v1"),
            self.repo.paths,
        )
        assert out_v1 is not None
        self.assertIn("v1 body", out_v1["hookSpecificOutput"]["additionalContext"])

        # Upgrade: registry now points at 0.2.0. Use a fresh session_id so
        # dedup doesn't suppress the second injection.
        self.repo.write_installed_plugins(
            _registry_with(("plug@mp", _record(new, version="0.2.0")))
        )
        out_v2 = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="s-v2"),
            self.repo.paths,
        )
        assert out_v2 is not None
        ctx = out_v2["hookSpecificOutput"]["additionalContext"]
        self.assertIn("v2 body", ctx)
        self.assertNotIn("v1 body", ctx)


# ---------- Precedence: registry vs marketplace_dir ----------


class RegistryVsMarketplaceDirPrecedenceTests(_RepoTestCase):
    def test_registry_overrides_marketplace_dir_when_both_present(self) -> None:
        """When the registry exists, the legacy marketplace_dir is NOT scanned.

        Otherwise marketplace authors who never installed the plugin would
        leak rules into every session.
        """
        # Marketplace tree carries a dummy plugin with rules — these MUST NOT load.
        self.repo.write_rules({"rules": [_rule(id="from-marketplace", content="should not load")]})

        # Registry points at a separate install with different rules.
        install = self.repo.make_install_dir("mp", "official", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="from-registry", content="this loads")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("official@mp", _record(install))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("this loads", ctx)
        self.assertNotIn("should not load", ctx)

    def test_no_registry_falls_back_to_marketplace_dir_scan(self) -> None:
        """Registry file absent → legacy scan still works (preserves dev workflow)."""
        # Ensure registry file is NOT created.
        self.assertFalse(self.repo.installed_plugins_file.exists())

        self.repo.write_rules({"rules": [_rule(id="legacy", content="legacy fallback body")]})
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("legacy fallback body", out["hookSpecificOutput"]["additionalContext"])

    def test_empty_registry_does_not_fall_back_to_marketplace_dir(self) -> None:
        """If the registry file exists but has no plugins, we treat it as
        authoritative — the user just hasn't installed any plugins."""
        self.repo.write_rules(
            {"rules": [_rule(id="legacy", content="should not load via fallback")]}
        )
        self.repo.write_installed_plugins({"version": 2, "plugins": {}})
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)


# ---------- Malformed registry handling ----------


class RegistryMalformedTests(_RepoTestCase):
    def test_invalid_json_logs_and_yields_no_sources(self) -> None:
        self.repo.write_installed_plugins_raw("{not valid json")
        self.repo.write_file("src/a.py")
        # Even though marketplace_dir has rules, the registry exists (just
        # malformed) so we must NOT fall back to it.
        self.repo.write_rules({"rules": [_rule(id="legacy", content="should not load")]})
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)

    def test_empty_string_registry_yields_no_sources_silently(self) -> None:
        self.repo.write_installed_plugins_raw("")
        self.repo.write_file("src/a.py")
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)

    def test_missing_plugins_key_is_treated_as_empty(self) -> None:
        self.repo.write_installed_plugins({"version": 2})
        self.repo.write_file("src/a.py")
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)

    def test_plugins_value_not_a_dict_is_ignored(self) -> None:
        self.repo.write_installed_plugins({"version": 2, "plugins": ["not", "a", "dict"]})
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        self.assertIsNone(out)

    def test_records_value_not_a_list_is_skipped(self) -> None:
        install = self.repo.make_install_dir("mp", "good", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="good", content="good body")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            {
                "version": 2,
                "plugins": {
                    "broken@mp": "not-a-list",
                    "good@mp": [_record(install)],
                },
            }
        )
        self.repo.write_file("src/a.py")
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("good body", out["hookSpecificOutput"]["additionalContext"])

    def test_record_not_a_dict_is_skipped(self) -> None:
        install = self.repo.make_install_dir("mp", "good", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="good", content="present")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            {
                "version": 2,
                "plugins": {
                    "good@mp": ["not-a-dict", _record(install)],
                },
            }
        )
        self.repo.write_file("src/a.py")
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("present", out["hookSpecificOutput"]["additionalContext"])

    def test_record_with_missing_install_path_is_skipped(self) -> None:
        install = self.repo.make_install_dir("mp", "good", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="good", content="present")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            {
                "version": 2,
                "plugins": {
                    "no-path@mp": [{"scope": "user", "version": "1.0.0"}],
                    "good@mp": [_record(install)],
                },
            }
        )
        self.repo.write_file("src/a.py")
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("present", out["hookSpecificOutput"]["additionalContext"])

    def test_record_with_non_string_install_path_is_skipped(self) -> None:
        install = self.repo.make_install_dir("mp", "good", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="good", content="present")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            {
                "version": 2,
                "plugins": {
                    "weird@mp": [{"scope": "user", "installPath": 42, "version": "1.0.0"}],
                    "good@mp": [_record(install)],
                },
            }
        )
        self.repo.write_file("src/a.py")
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("present", out["hookSpecificOutput"]["additionalContext"])

    def test_record_with_unresolvable_install_path_is_skipped(self) -> None:
        """An `installPath` with an embedded NUL makes `Path.resolve()` raise
        `ValueError` (not `OSError`) on Python 3.14. The record is skipped and
        the rest of the registry is still walked — one poisoned path must not
        take down discovery for every other plugin."""
        install = self.repo.make_install_dir("mp", "good", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="good", content="present")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            {
                "version": 2,
                "plugins": {
                    "nul@mp": [
                        {
                            "scope": "user",
                            "installPath": f"{install}\x00stale",
                            "version": "1.0.0",
                        }
                    ],
                    "good@mp": [_record(install)],
                },
            }
        )
        self.repo.write_file("src/a.py")
        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("present", out["hookSpecificOutput"]["additionalContext"])


# ---------- Rule semantics layered on registry-discovered sources ----------


class RegistryRuleSemanticsTests(_RepoTestCase):
    def test_activation_criteria_evaluated_for_registry_source(self) -> None:
        install = self.repo.make_install_dir("mp", "criteria", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps(
                {
                    "activation_criteria": {"any_file_exists": "marker.txt"},
                    "rules": [_rule(id="r1", content="conditional body")],
                }
            ),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("criteria@mp", _record(install))))
        self.repo.write_file("src/a.py")

        # Without marker.txt, activation_criteria fails → no rules.
        out_no = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="s1"),
            self.repo.paths,
        )
        self.assertIsNone(out_no)

        # With marker.txt, the rule fires.
        self.repo.write_file("marker.txt", "x")
        out_yes = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="s2"),
            self.repo.paths,
        )
        assert out_yes is not None
        self.assertIn("conditional body", out_yes["hookSpecificOutput"]["additionalContext"])

    def test_content_file_resolved_relative_to_install_path(self) -> None:
        install = self.repo.make_install_dir("mp", "cf", "1.0.0")
        (install / "rules" / "guidance.md").parent.mkdir(parents=True, exist_ok=True)
        (install / "rules" / "guidance.md").write_text("guidance from cache", encoding="utf-8")
        (install / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "cf",
                            "when": {"triggering_file_path_glob": "src/*.py"},
                            "content_file": "rules/guidance.md",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("cf@mp", _record(install))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("guidance from cache", out["hookSpecificOutput"]["additionalContext"])

    def test_content_file_escaping_install_path_is_config_error(self) -> None:
        install = self.repo.make_install_dir("mp", "esc", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "esc",
                            "when": {"triggering_file_path_glob": "src/*.py"},
                            "content_file": "../../etc/passwd",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("esc@mp", _record(install))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("escapes plugin root", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_audit_log_marks_registry_rules_as_marketplace(self) -> None:
        """Rules from the registry are still marketplace rules in the audit
        log — `is_marketplace_rule=True`, `is_project_rule=False`."""
        install = self.repo.make_install_dir("mp", "audit", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="audit", content="x")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("audit@mp", _record(install))))
        self.repo.write_file("src/a.py")

        cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py", session_id="audit-sess"),
            self.repo.paths,
        )

        cache = cr.load_cache(self.repo.paths.state_dir / "audit-sess.json")
        self.assertIn("audit", cache)
        self.assertTrue(cache["audit"]["is_marketplace_rule"])
        self.assertFalse(cache["audit"]["is_project_rule"])


# ---------- Cross-source: registry + project rules together ----------


class RegistryAndProjectRulesTests(_RepoTestCase):
    def test_registry_and_project_rules_both_inject(self) -> None:
        install = self.repo.make_install_dir("mp", "from-mp", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="mp-rule", content="from registry")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("from-mp@mp", _record(install))))

        proj_rules = self.repo.project_root / ".claude" / "conditional_rules"
        proj_rules.mkdir(parents=True, exist_ok=True)
        (proj_rules / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="proj-rule", content="from project")]}),
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
        self.assertIn("from registry", ctx)
        self.assertIn("from project", ctx)


# ---------- End-to-end via entry script ----------


class RegistryEntryScriptTests(_RepoTestCase):
    def test_pre_script_uses_registry_when_env_var_points_at_it(self) -> None:
        install = self.repo.make_install_dir("mp", "e2e", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="e2e", content="end-to-end body")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("e2e@mp", _record(install))))
        self.repo.write_file("src/a.py")

        runner = _EntryScriptRunner(
            "conditional_rules_pre.py",
            self.repo.plugin_root,
            self.repo.project_root,
            self.repo.marketplace_dir,
            installed_plugins_file=self.repo.installed_plugins_file,
        )
        rc, out, err = runner.run(
            json.dumps(self.repo.make_input(file_rel="src/a.py", session_id="e2e-sess"))
        )
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertIn("end-to-end body", payload["hookSpecificOutput"]["additionalContext"])


# ---------- Direct unit tests for _discover_from_registry ----------


class DiscoverFromRegistryUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RepoFixture()
        self.addCleanup(self.repo.cleanup)

    def _discover(self) -> list:
        """Discover against the fixture's registry and project root."""
        return cr._discover_from_registry(self.repo.installed_plugins_file, self.repo.project_root)

    def test_returns_empty_when_file_missing(self) -> None:
        ghost = self.repo.installed_plugins_file.parent / "does-not-exist.json"
        self.assertEqual(cr._discover_from_registry(ghost, self.repo.project_root), [])

    def test_dedup_by_resolved_path_handles_relative_install_path_collision(self) -> None:
        """Two records pointing at the same physical dir via different
        spellings (with/without trailing slash) should still dedupe.

        The project-scope record names the current project so it clears the
        scope gate and dedup is what removes it, not the gate.
        """
        install = self.repo.make_install_dir("mp", "shared", "1.0.0")
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="dedup", content="x")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            {
                "version": 2,
                "plugins": {
                    "shared@mp": [
                        {"scope": "user", "installPath": str(install), "version": "1.0.0"},
                        {
                            "scope": "project",
                            "projectPath": str(self.repo.project_root),
                            "installPath": str(install) + "/",
                            "version": "1.0.0",
                        },
                    ]
                },
            }
        )
        sources = self._discover()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].rules_file, install / "rules.json")
        self.assertTrue(sources[0].check_activation)

    def test_finds_rules_under_rules_subdir(self) -> None:
        """Real-world layout used by every plugin in this repo:
        `<installPath>/rules/rules.json`."""
        install = self.repo.make_install_dir("mp", "subdir-layout", "1.0.0")
        rules_dir = install / "rules"
        rules_dir.mkdir()
        (rules_dir / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="found-in-subdir", content="hi")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("subdir-layout@mp", _record(install))))
        sources = self._discover()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].rules_file, rules_dir / "rules.json")
        # content_root is the directory containing rules.json, so adjacent .md
        # files (e.g. rules/api-patterns.md) resolve correctly.
        self.assertEqual(sources[0].content_root, rules_dir)

    def test_finds_rules_under_dot_conditional_rules_subdir(self) -> None:
        install = self.repo.make_install_dir("mp", "dot-layout", "1.0.0")
        rules_dir = install / ".conditional_rules"
        rules_dir.mkdir()
        (rules_dir / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="dot", content="hi")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("dot-layout@mp", _record(install))))
        sources = self._discover()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].rules_file, rules_dir / "rules.json")

    def test_root_layout_takes_precedence_over_subdir(self) -> None:
        """If a plugin somehow ships both `rules.json` and `rules/rules.json`,
        the root one wins (matches the candidate-list order)."""
        install = self.repo.make_install_dir("mp", "both", "1.0.0")
        (install / "rules").mkdir()
        (install / "rules" / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="from-sub", content="sub")]}),
            encoding="utf-8",
        )
        (install / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="from-root", content="root")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("both@mp", _record(install))))
        sources = self._discover()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].rules_file, install / "rules.json")

    def test_content_file_with_nested_path_resolves_under_rules_subdir(self) -> None:
        """When `content_file` is a nested path like `xpto/qwerty/123.md`,
        it resolves under the directory that contains rules.json (the
        `rules/` subdirectory), not under installPath itself."""
        install = self.repo.make_install_dir("mp", "nested", "1.0.0")
        rules_dir = install / "rules"
        rules_dir.mkdir()
        nested_dir = rules_dir / "xpto" / "qwerty"
        nested_dir.mkdir(parents=True)
        (nested_dir / "123.md").write_text("deep body", encoding="utf-8")
        (rules_dir / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "deep",
                            "when": {"triggering_file_path_glob": "src/*.py"},
                            "content_file": "xpto/qwerty/123.md",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("nested@mp", _record(install))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("deep body", out["hookSpecificOutput"]["additionalContext"])
        # systemMessage uses the content-root-relative label.
        self.assertIn("xpto/qwerty/123.md", out["systemMessage"])

    def test_content_file_escaping_rules_subdir_is_rejected(self) -> None:
        """`content_file: "../foo.md"` from inside `rules/` would resolve to
        `<installPath>/foo.md` — outside the rules.json directory — and must
        be rejected as a config error, blocking the tool with `deny`."""
        install = self.repo.make_install_dir("mp", "escape", "1.0.0")
        rules_dir = install / "rules"
        rules_dir.mkdir()
        # Create the would-be target outside `rules/` to prove that the
        # rejection happens at config-load time, not based on file presence.
        (install / "outside.md").write_text("outside body", encoding="utf-8")
        (rules_dir / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "esc",
                            "when": {"triggering_file_path_glob": "src/*.py"},
                            "content_file": "../outside.md",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("escape@mp", _record(install))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "escapes plugin root",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_content_file_resolves_relative_to_rules_subdir(self) -> None:
        """Regression for the real-world bug: when rules live in
        `<installPath>/rules/rules.json`, sibling `.md` content files in
        `rules/` must resolve. content_root is the directory containing
        rules.json, not installPath itself."""
        install = self.repo.make_install_dir("mp", "real", "1.0.0")
        rules_dir = install / "rules"
        rules_dir.mkdir()
        (rules_dir / "guidance.md").write_text("body in subdir", encoding="utf-8")
        (rules_dir / "rules.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "subdir-cf",
                            "when": {"triggering_file_path_glob": "src/*.py"},
                            "content_file": "guidance.md",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(_registry_with(("real@mp", _record(install))))
        self.repo.write_file("src/a.py")

        out = cr.handle(
            "PreToolUse",
            self.repo.make_input(file_rel="src/a.py"),
            self.repo.paths,
        )
        assert out is not None
        self.assertIn("body in subdir", out["hookSpecificOutput"]["additionalContext"])

    def test_plugin_keys_processed_in_sorted_order(self) -> None:
        """Stable iteration matters for predictable rule ordering in tests."""
        a = self.repo.make_install_dir("mp", "alpha", "1.0.0")
        b = self.repo.make_install_dir("mp", "bravo", "1.0.0")
        (a / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="alpha-rule", content="a")]}),
            encoding="utf-8",
        )
        (b / "rules.json").write_text(
            json.dumps({"rules": [_rule(id="bravo-rule", content="b")]}),
            encoding="utf-8",
        )
        self.repo.write_installed_plugins(
            {
                "version": 2,
                "plugins": {
                    # Insertion order intentionally reverse-sorted.
                    "bravo@mp": [_record(b)],
                    "alpha@mp": [_record(a)],
                },
            }
        )
        sources = self._discover()
        self.assertEqual(
            [s.rules_file.parent.name for s in sources],
            ["1.0.0", "1.0.0"],
        )
        # Path order should be alpha first, bravo second (sorted plugin keys).
        self.assertIn("alpha", str(sources[0].rules_file))
        self.assertIn("bravo", str(sources[1].rules_file))


if __name__ == "__main__":
    unittest.main()
