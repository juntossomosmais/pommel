# Invalid: structured-logging message template

- Rewrite the `{Placeholder}` template as a fully formatted string: `_logger.LogInformation($"Created address {address.Id}");`, never `_logger.LogInformation("Created address {AddressId}", address.Id);`.
- Keep the exception argument when present: `_logger.LogError(ex, $"Failed to process order {order.Id}");`.
- Log attributes, never whole objects (entities, DTOs, messages).
- Convert every templated log call in this file, not only the one just written.
