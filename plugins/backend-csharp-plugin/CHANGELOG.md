# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-17

### Added
- Initial public release: a conditional rule pack for C# / .NET backends, activated only in projects with a `.sln` or `.csproj`.
- Always-on SDLC rules (`main-rules`): containerized build/test/format commands, coding conventions, data-access rules (direct `DbContext`, `.AsNoTracking()`, batched queries, explicit transactions), and CAP messaging rules with transactional publishing.
- Path- and content-triggered rules: ASP.NET Core controller conventions on `NDjango.RestFramework` (`api-patterns`), Hangfire background jobs (`hangfire-jobs`), CAP consumers with Ziggurat (`messaging-cap-consumers`), `System.CommandLine` entry points and Startup wiring (`program-and-commands`), serializer placement and versioning (`serializers`), service/SDK layering with typed HttpClient and per-method resilience (`services-and-sdks`), and xUnit/Moq testing conventions (`testing`).
- Reactive `PostToolUse` rules that fire only when just-written content violates a convention: CAP publish with no transaction in the same file, publish outside the serializer layer, unversioned API routes/namespaces, DbContext writes in controllers, structured-logging templates, raw `[CapSubscribe]` topic strings, repository-layer reintroduction (gated on `NDjango.RestFramework` in the lockfile), and FluentAssertions in tests.
- Dependency on `conditional-rules-plugin` (>=0.1.0), installed automatically.
