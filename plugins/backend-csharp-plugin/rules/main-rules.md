# SDLC

- **Always create tests** for every implementation.
- **Building the solution**: By default, use the filter script to get condensed output:
    ```shell
    docker compose run --volume "$(PWD):/app" --rm --remove-orphans integration-tests bash -c 'dotnet build [PutHereProjectSolutionName].sln > /tmp/build-output.txt 2>&1; cat /tmp/build-output.txt | dotnet dotnet-script ./scripts/filter-build-output.csx'
    ```
  When you need the raw unfiltered output (e.g., to debug a build issue or inspect restore details), run without the script:
    ```shell
    docker compose run --volume "$(PWD):/app" --rm --remove-orphans integration-tests bash -c 'dotnet build [PutHereProjectSolutionName].sln'
    ```
- Run selective unit testing focusing on the classes you have changed. Always on classes, not on methods:
    ```shell
    docker compose run --volume "$(PWD):/app" --rm --remove-orphans integration-tests bash -c 'dotnet test [PutHereProjectSolutionName].sln --settings "./runsettings.xml" --filter "TheClassYouWantToTest" > /tmp/test-output.txt 2>&1; cat /tmp/test-output.txt | dotnet dotnet-script ./scripts/filter-failed-tests.csx'
    ```
- **Coverage for selective testing**: Sample commands to check coverage for a specific file:
    ```bash
    docker compose run --volume "$(PWD):/app" --rm --remove-orphans integration-tests bash -c 'dotnet test [PutHereProjectSolutionName].sln --settings "./runsettings.xml" --filter "TheClassYouWantToTest" > /tmp/test-output.txt 2>&1; cat /tmp/test-output.txt | dotnet dotnet-script ./scripts/generate-coverage-report.csx -- "SampleClass"'
    ```
- Run all tests when the selective runs are successful to ensure overall integrity (you MUST execute exactly like this):
    ```shell
    docker compose run --volume "$(PWD):/app" --rm --remove-orphans integration-tests bash -c 'dotnet test [PutHereProjectSolutionName].sln --settings "./runsettings.xml" > /tmp/test-output.txt 2>&1; cat /tmp/test-output.txt | dotnet dotnet-script ./scripts/filter-failed-tests.csx'
    ```
- When the implementation is fully completed, you can format the code with (you don't need to execute tests after this):
    ```shell
    docker compose run --volume "$(PWD):/app" --rm --remove-orphans lint-formatter dotnet format
    ```

**Important:** Do not pipe `dotnet test` directly into `dotnet dotnet-script` inside the container. Concurrent `dotnet` processes cause coverlet to produce empty coverage data. Always save output to a temp file first, then pipe.

## Coding conventions

- Use "Async" suffix in names of methods that return an awaitable type

## Data Access

- **Inject `SqlContext` directly** into controllers and service classes. Do not go through a repository layer for new code.
- **Never create new repository classes/interfaces.**
- **Always add `.AsNoTracking()`** on read-only queries. Omitting it is a performance bug — EF Core will track every loaded entity unnecessarily.
- **Use `ExecuteDeleteAsync()` / `ExecuteUpdateAsync()`** for bulk operations without loading entities into memory first.
- When validating or fetching data for a collection of items, always use a single batched query instead of calling the database once per item.
- Wrap multi-step writes in an explicit transaction block. Use await using `(var tx = await SqlContext.Database.BeginTransactionAsync()) { ... await tx.CommitAsync(); }`. The block makes the commit/rollback scope visible at a glance and prevents the transaction from accidentally outliving the operation it's meant to protect.

## Messaging — CAP Publishing

- **Never call `PublishAsync` outside a transaction when there is any accompanying DB writing.** Doing so creates a race condition: the DB write can succeed while the message is lost (or vice versa). This applies to both command handlers and consumer methods.
  - **There are no exceptions to this rule.** Even a single `SaveChangesAsync` in the same logical operation requires the transaction.

## Messaging - CAP

It defines the structure of RabbitMQ message payloads and the routing key registry that binds topics to consumer groups.

### Rules

- **All topic key constants should live in a dedicated `cs` file as `public const string` fields. Never use a raw string as a topic name.
- Topic names follow the pattern `[source-system-name-prefix].[domain].[event]` (e.g., `billing-service.payment.created`)
- Group names follow the pattern `[your-app].[domain].[event]` (e.g., `my-app.payment.created`).
  - Subscription groups allow you to load-balance message processing across multiple instances of a service. By default, CAP uses the assembly name as the group name. If multiple subscribers in the same group subscribe to the same topic, only one will receive the message (competing consumers). If they are in different groups, all will receive the message (fan-out).
- Message classes must implement `Ziggurat.IMessage` which requires `string MessageId { get; set; }` and `string MessageGroup { get; set; }`. It should be used as your payload DTO.
- Failures from infrastructure or external dependencies (e.g., Redis, databases, HTTP services, queues) must be mapped to 5xx responses (prefer 503 for unavailability). Do not convert these errors to 4xx. Return 4xx only for client-caused issues (invalid input, contract violations).
- Do not use structured logging. Avoid message templates with {} placeholders. Always log fully formatted strings (string interpolation or concatenation).
- NEVER log entire objects, only their attributes, no more than that
- If a method has `return null;` in any branch, its signature must be `T?`. If a parameter has `default = null`, the type must be `T?`. Suppressing CS8601/CS8625 with `!` is forbidden unless a comment explains the invariant that the compiler cannot prove. Run `dotnet build` and address any CS8xxx warnings.