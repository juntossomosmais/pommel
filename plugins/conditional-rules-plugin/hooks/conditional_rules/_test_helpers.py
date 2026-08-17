"""Shared fixtures, helpers, and base classes for the conditional-rules test suite."""

from __future__ import annotations

import io
import json
import os
import runpy
import shutil
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import conditional_rules as cr

# ---------- Declarative tree builder ----------


@contextmanager
def create_structure(
    structure: dict,
    *,
    base_dir: Path | str | None = None,
) -> Iterator[tuple[Path, list[Path]]]:
    """Materialize a nested file/directory structure in a temp location."""
    if base_dir is None:
        root = Path(tempfile.mkdtemp(prefix="tmp-conditional-rules-"))
        owns_root = True
    else:
        root = Path(base_dir)
        root.mkdir(parents=True, exist_ok=True)
        owns_root = False

    created: list[Path] = []
    try:
        _populate(root, structure, created)
        yield root, created
    finally:
        if owns_root:
            shutil.rmtree(root, ignore_errors=True)


def _populate(folder: Path, structure: dict, created: list[Path]) -> None:
    for name, value in structure.items():
        target = folder / name
        if isinstance(value, dict):
            target.mkdir(parents=True, exist_ok=True)
            _populate(target, value, created)
        elif isinstance(value, str):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="utf-8")
            created.append(target)
        else:
            raise TypeError(
                f"create_structure: value for {name!r} must be str (file content) or dict (nested), "
                f"got {type(value).__name__}"
            )


# ---------- Fixture helpers ----------


def _rule(**overrides) -> dict:
    """Build a minimal valid rule, with overrides applied."""
    base = {
        "id": "r1",
        "when": {"triggering_file_path_glob": "src/*.py"},
        "content": "default content",
    }
    base.update(overrides)
    return base


class _RepoFixture:
    """A tmp-dir 'plugin + project + marketplace' layout.

    Mirrors the installed shape:

        <tmp>/plugin/                       ← plugin_root (state_dir only)
        <tmp>/marketplace/                  ← marketplace_dir (legacy fallback scan)
        <tmp>/marketplace/test-plugin/      ← single test plugin
        <tmp>/marketplace/test-plugin/rules.json  ← content_root / rules_file
        <tmp>/installed_plugins.json        ← optional registry (not created by default)
        <tmp>/project/                      ← project_root

    The fixture defaults `installed_plugins_file` to a path inside the tmp
    that does NOT exist, so existing tests fall through to the marketplace
    scan (legacy behavior) and never read the user's real registry. Tests
    that exercise registry-based discovery must call
    `write_installed_plugins(...)` first.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()

        # plugin_root used only for state_dir
        self.plugin_root = base / "plugin"
        self.project_root = base / "project"

        # Marketplace discovery root (legacy fallback)
        self.marketplace_dir = base / "marketplace"
        # Single test plugin within the marketplace
        _plugin_dir = self.marketplace_dir / "test-plugin"
        # content_root is where rules.json lives and where content files are resolved from
        self.content_root = _plugin_dir
        self.rules_file = _plugin_dir / "rules.json"

        # Registry-based discovery target. Path is inside the tmp so it never
        # collides with the real user registry. Tests opt in by writing here.
        self.installed_plugins_file = base / "installed_plugins.json"

        # User-scope settings file used for `enabledPlugins` filtering. Tests
        # opt in via `write_user_settings(...)`; if not written, the file is
        # absent and no plugin keys are filtered.
        self.user_settings_file = base / "user-settings.json"

        self.paths = cr.Paths.for_test(
            self.plugin_root,
            self.project_root,
            marketplace_dir=self.marketplace_dir,
            installed_plugins_file=self.installed_plugins_file,
            user_settings_file=self.user_settings_file,
        )
        _plugin_dir.mkdir(parents=True, exist_ok=True)
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        (self.project_root / "src").mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Compatibility alias — tests that reason about 'the user's repo' use this."""
        return self.project_root

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def write_rules(self, data: dict) -> None:
        self.rules_file.write_text(json.dumps(data), encoding="utf-8")

    def write_rules_raw(self, text: str) -> None:
        self.rules_file.write_text(text, encoding="utf-8")

    def write_installed_plugins(self, data: dict) -> None:
        """Write a registry file at `installed_plugins_file` (opt-in).

        Pass the full top-level dict — typically:
            {"version": 2, "plugins": {"<plugin>@<marketplace>": [{...}, ...]}}
        """
        self.installed_plugins_file.parent.mkdir(parents=True, exist_ok=True)
        self.installed_plugins_file.write_text(json.dumps(data), encoding="utf-8")

    def write_installed_plugins_raw(self, text: str) -> None:
        """Write arbitrary bytes at `installed_plugins_file` (for malformed-input tests)."""
        self.installed_plugins_file.parent.mkdir(parents=True, exist_ok=True)
        self.installed_plugins_file.write_text(text, encoding="utf-8")

    def write_user_settings(self, data: dict) -> None:
        """Write a settings.json shape at `user_settings_file` (opt-in).

        Pass the full top-level dict; the only key the hook reads is
        `enabledPlugins`, mapping `<plugin>@<marketplace>` to bool.
        """
        self.user_settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.user_settings_file.write_text(json.dumps(data), encoding="utf-8")

    def write_user_settings_raw(self, text: str) -> None:
        """Write arbitrary bytes at `user_settings_file` (for malformed-input tests)."""
        self.user_settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.user_settings_file.write_text(text, encoding="utf-8")

    def write_project_settings(self, data: dict) -> None:
        """Write `<project>/.claude/settings.json` (opt-in)."""
        path = self.project_root / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def write_project_local_settings(self, data: dict) -> None:
        """Write `<project>/.claude/settings.local.json` (opt-in)."""
        path = self.project_root / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def make_install_dir(self, marketplace: str, plugin: str, version: str) -> Path:
        """Create an install-cache directory under the tmp and return its path.

        Mirrors `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`.
        Useful for asserting that registry-based discovery picks the version
        the registry points at, not stale ones lying next to it.
        """
        install_dir = self._tmp_root() / "cache" / marketplace / plugin / version
        install_dir.mkdir(parents=True, exist_ok=True)
        return install_dir

    def _tmp_root(self) -> Path:
        return Path(self._tmp.name).resolve()

    def write_project_file(self, rel: str, content: str = "") -> Path:
        p = self.project_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    # Kept for test clarity when the subject is really the user's project file.
    def write_file(self, rel: str, content: str = "") -> Path:
        return self.write_project_file(rel, content)

    def write_plugin_file(self, rel: str, content: str = "") -> Path:
        """Write a content file into the test plugin's content_root."""
        p = self.content_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def make_input(
        self,
        *,
        file_rel: str | None,
        tool_name: str = "Edit",
        session_id: str = "sess-1",
        event: str = "PreToolUse",
        agent_id: str | None = None,
    ) -> dict:
        tool_input: dict = {}
        if file_rel is not None:
            tool_input["file_path"] = str(self.project_root / file_rel)
        payload: dict = {
            "session_id": session_id,
            "hook_event_name": event,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": str(self.project_root),
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        return payload


class _RepoTestCase(unittest.TestCase):
    """Base class that provisions a per-test _RepoFixture."""

    def setUp(self) -> None:
        self.repo = _RepoFixture()
        self.addCleanup(self.repo.cleanup)


# ---------- Entry-script runner ----------


class _EntryScriptRunner:
    """Helper to execute one of the wrapper scripts with a fake stdin/stdout.

    `installed_plugins_file` defaults to a path that does NOT exist so the
    legacy `marketplace_dir` fallback is taken. Tests that exercise the
    registry path must pass an existing file.
    """

    def __init__(
        self,
        script_name: str,
        plugin_root: Path,
        project_root: Path,
        marketplace_dir: Path,
        installed_plugins_file: Path | None = None,
        user_settings_file: Path | None = None,
    ) -> None:
        self.script = HERE / script_name
        self.plugin_root = plugin_root
        self.project_root = project_root
        self.marketplace_dir = marketplace_dir
        self.installed_plugins_file = (
            installed_plugins_file
            if installed_plugins_file is not None
            else plugin_root / "_no_registry_.json"
        )
        self.user_settings_file = (
            user_settings_file
            if user_settings_file is not None
            else plugin_root / "_no_user_settings_.json"
        )

    def run(self, stdin_text: str) -> tuple[int, str, str]:
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        env_saves = {
            "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
            "CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT"),
            "CLAUDE_MARKETPLACE_DIR": os.environ.get("CLAUDE_MARKETPLACE_DIR"),
            "CLAUDE_INSTALLED_PLUGINS_FILE": os.environ.get("CLAUDE_INSTALLED_PLUGINS_FILE"),
            "CLAUDE_USER_SETTINGS_FILE": os.environ.get("CLAUDE_USER_SETTINGS_FILE"),
        }
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project_root)
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(self.plugin_root)
        os.environ["CLAUDE_MARKETPLACE_DIR"] = str(self.marketplace_dir)
        os.environ["CLAUDE_INSTALLED_PLUGINS_FILE"] = str(self.installed_plugins_file)
        os.environ["CLAUDE_USER_SETTINGS_FILE"] = str(self.user_settings_file)
        rc = 0
        try:
            try:
                runpy.run_path(str(self.script), run_name="__main__")
            except SystemExit as exc:
                rc = int(exc.code) if exc.code is not None else 0
            return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr
            for name, prev in env_saves.items():
                if prev is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = prev


# ---------- E2E input builder ----------


def _make_e2e_input(
    project_root: Path,
    file_rel: str | None,
    *,
    session_id: str = "e2e-1",
    tool: str = "Edit",
    event: str = "PreToolUse",
) -> dict:
    tool_input: dict = {}
    if file_rel is not None:
        tool_input["file_path"] = str(project_root / file_rel)
    return {
        "session_id": session_id,
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": str(project_root),
    }
