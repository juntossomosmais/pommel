# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-17

### Added
- Initial public release: a hook engine that injects context-aware rules at `PreToolUse`, `PostToolUse`, and `SessionStart`, with path-glob/regex and content-regex predicates on the triggering file, project-shape checks (`any_file_exists`, `any_file_content_regex`), `all_of`/`any_of`/`not` combinators, per-plugin `activation_criteria`, tool filters, and per-session dedup with an audit log.
- `additionalContext` opens with a one-line manifest naming every fired rule, so hook output truncated by Claude Code's 10,000-character cap stays detectable and recoverable from the persisted file.
- `manage-rules` command for authoring conventional and conditional rules.