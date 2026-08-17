# Invalid: CAP publish with no transaction in this file

This file calls `PublishAsync` but never `BeginTransactionAsync`.

- Wrap the DB write and the publish in one transaction: `await using var tx = await _dbContext.Database.BeginTransactionAsync(_capPublisher, autoCommit: false, cancellationToken: cancellationToken);` … `await tx.CommitAsync(cancellationToken);`.
- Pass `_capPublisher` — a plain EF transaction does not enlist the CAP outbox.
- In a Consumer, Controller, or Job, do not publish at all: move the publish into the entity's `Serializer<>` CRUD override.
- If no DB write shares this logical operation, state that explicitly and leave the code as is.
- Audit every `PublishAsync` in this file, not only the one just written.
