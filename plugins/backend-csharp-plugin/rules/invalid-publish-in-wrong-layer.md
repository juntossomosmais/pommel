# Invalid: PublishAsync outside the serializer layer

- Do not publish from a Consumer, Controller, or Job.
- Move the publish into the entity's `Serializer<>` CRUD override, inside its `BeginTransactionAsync(_capPublisher, autoCommit: false)` block, and call that serializer method from here instead.
- Move every publish call out of this file, not only the one just written.
