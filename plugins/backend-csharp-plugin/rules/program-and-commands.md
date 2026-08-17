# Program and Commands

The application uses **System.CommandLine** to define CLI commands. Commands are registered as `Command` instances in `Program.BuildRootCommand()`, which creates a `RootCommand` with four subcommands: `api`, `worker`, `job-worker`, and `seed`.

## Application entry point (`Program.cs`)

- `Main` builds the `RootCommand` via `BuildRootCommand()`, parses the args, and invokes the matched command.
- `BuildRootCommand()` is `public static` so tests can parse args and inspect `ParseResult.Errors` without invoking.
- `TopicNameOption` is a `public static readonly Option<string>` on `Program` — the `worker` subcommand's `--topic-name` required option.
- Three host builders exist:
  - `CreateHostBuilder` — used by the `api` command, wires `ApiCommand.Startup`.
  - `CreateConsumerHostBuilder` — used by the `worker` command, wires `ConsumerCommand.Startup`.
  - `CreateJobWorkerHostBuilder` — used by the `job-worker` command, wires `WorkerCommand.Startup`.
- `BuildConfiguration` loads `appsettings.json` and environment variables. It is the single source of configuration.

## Shared services (`ConfigureSharedServices`)

All host command Startup classes (`ApiCommand.Startup`, `ConsumerCommand.Startup`, `WorkerCommand.Startup`) call `services.ConfigureSharedServices(configuration)`. This extension method registers:

- **AppDbContext** via `UseSqlServer`.
- **CAP + RabbitMQ** — message persistence with SQL Server, transport with RabbitMQ.
- **`BootstrapFilter`** via `.AddSubscribeFilter<BootstrapFilter>()`. This Ziggurat filter is **mandatory** — it enables Ziggurat's consumer pipeline for CAP. Never remove it.
- **Health checks** — SQL Server (tagged `crucial`), RabbitMQ, and Redis.
- **OpenTelemetry** — runtime metrics, HTTP client instrumentation, SQL client instrumentation, OTLP exporter.

Ensure these registrations remain outside of individual Startup classes, which should contain only host-specific services

Each SDK file exposes its own `Add<SdkName>` extension method (e.g., `services.AddIdentitySDK(configuration)`). `ConfigureSharedServices` calls these as one-liners.

## Shared middleware (`ApplicationBuilderExtensions`)

`UseStandardHealthChecks()` is an extension method on `IApplicationBuilder` defined in `Program.cs`. It configures the three Kubernetes health check endpoints required by all host commands:

- a liveness endpoint — always healthy (predicate `_ => false`), no checks executed.
- a readiness endpoint — runs only checks tagged `"crucial"`.
- an integrations endpoint — runs all registered checks.

Every host command's `Startup.Configure` must call `app.UseStandardHealthChecks()`. Do not inline health check endpoint configuration in individual Startup classes.

## Conventions

- **Host commands** (`api`, `worker`, `job-worker`) must have a **nested `Startup` class** with `ConfigureServices` and `Configure` methods. Both must call `ConfigureSharedServices` first, and `Configure` must call `app.UseStandardHealthChecks()`.
- **Non-host commands** (like `seed`) do not need a Startup class — they operate directly.
- **Command actions** are defined as lambdas in `BuildRootCommand()`. Command classes hold static execution methods (accepting `TextWriter` for output/error) and nested `Startup` classes.
- **FluentValidation registrations are manual.** Each validator must be explicitly registered as `services.AddScoped<IValidator<T>, TValidation>()` in the relevant Startup class. Do not use assembly scanning.
- **One worker per topic.** Workers are deployed with `dotnet run worker --topic-name <topic>`. Each instance consumes a single topic.
- **Keep single-use classes nested inside their only consumer.** If a class (e.g., a configurator, helper, or strategy) is used exclusively by one command class, define it as a private nested class in that file. Do not create separate folders or files for it.
