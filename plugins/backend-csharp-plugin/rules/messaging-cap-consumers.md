# Messaging - CAP consumers

## File structure

Put each consumer in a **single file** under `src/Consumers/` containing all three concerns:

1. **Message DTO** — implement `Ziggurat.IMessage` (`MessageId`, `MessageGroup`), plus any nested payload DTOs.
2. **Consumer class** — implement `ICapSubscribe`. Keep it as a thin bridge.
3. **Nested `Handler` class** — implement `IConsumerService<TMessage>` and put all business logic here.

## Naming conventions

| Concern | Convention | Example |
|---|---|---|
| Message DTO | `{Event}Message` | `CreateAddressMessage` |
| Payload DTO | domain-specific name | `CreateStoreAddressPayload` |
| Consumer class | `{Event}Consumer` | `CreateAddressConsumer` |
| Nested handler | `{Consumer}.Handler` (always `Handler`) | `CreateAddressConsumer.Handler` |
| Subscription method | `HandleAsync` (always) | — |
| File name | `{Event}Consumer.cs` | `CreateAddressConsumer.cs` |

## Rules

- Ziggurat's `IMessage` interface is a consumer-side concern.
- Keep the outer consumer class rigid: inject only `IConsumerService<TMessage>`, expose only `HandleAsync` decorated with `[CapSubscribe]`, and delegate to `_consumerService.ProcessMessageAsync(message, cancellationToken)`. Add no other dependencies and no other logic. `HandleAsync` must accept `CancellationToken` and forward it.
- Put all business logic inside the nested `Handler` class, in `ProcessMessageAsync`. Do not extract business logic into a separate service class, unless the same logic is used in multiple places.
- Validate by calling the injected `Serializer<>`'s `RunValidationAsync` against the DTO. Do not write `AbstractValidator<T>` classes for messages — `NDjango.RestFramework` serializers are the single source of truth for validation.
- Map the message into the serializer's DTO, run `RunValidationAsync` with a `ValidationContext<TPrimaryKey>` matching the `SerializerOperation` you intend (`Create`, `Update`, `PartialUpdate`, `Destroy`), and check the `errors` dictionary. If validation fails, log a warning with the `message-id` and return — do not throw, do not retry.
- Persist and publish through the serializer's `CreateAsync` / `UpdateAsync` / `PartialUpdateAsync` / `DestroyAsync`. Do not call `DbContext` directly and do not call `PublishAsync` from the handler — the serializer owns transactions, CAP publish, Hangfire enqueue, and any other background tasks.
- Do not implement deduplication logic. Idempotency is provided by the registration pipeline via `UseEntityFrameworkIdempotency`.
- Use `Topics.*` and `Groups.*` constants for the `[CapSubscribe]` attribute — never inline strings.

## Example

```csharp
using DotNetCore.CAP;
using DotNetTemplate.Serializers.V1;
using Ziggurat;
using NDjango.RestFramework.Serializer;

namespace DotNetTemplate.Consumers;

public class CreateAddressMessage : IMessage
{
    public string MessageId { get; set; } = string.Empty;
    public string MessageGroup { get; set; } = string.Empty;
    public int StoreId { get; set; }
    public string Cep { get; set; } = string.Empty;
    public string Address { get; set; } = string.Empty;
    public string Number { get; set; } = string.Empty;
    public bool IsMain { get; set; }
}

public class CreateAddressConsumer : ICapSubscribe
{
    private readonly IConsumerService<CreateAddressMessage> _consumerService;

    public CreateAddressConsumer(IConsumerService<CreateAddressMessage> consumerService)
        => _consumerService = consumerService;

    [CapSubscribe(Topics.AddressCreated, Group = Groups.AddressCreated)]
    public async Task HandleAsync(CreateAddressMessage message, CancellationToken cancellationToken)
        => await _consumerService.ProcessMessageAsync(message, cancellationToken);

    public class Handler : IConsumerService<CreateAddressMessage>
    {
        private readonly AddressSerializer _serializer;
        private readonly ILogger<Handler> _logger;

        public Handler(AddressSerializer serializer, ILogger<Handler> logger)
        {
            _serializer = serializer;
            _logger = logger;
        }

        public async Task ProcessMessageAsync(
            CreateAddressMessage message,
            CancellationToken cancellationToken = default)
        {
            var dto = new AddressDto
            {
                StoreId = message.StoreId,
                Cep = message.Cep,
                AddressLine = message.Address,
                Number = message.Number,
                IsMain = message.IsMain
            };

            var errors = new Dictionary<string, List<string>>();
            var context = new ValidationContext<int>(SerializerOperation.Create, default);
            dto = await _serializer.RunValidationAsync(dto, context, errors, cancellationToken: cancellationToken);

            if (errors.Count > 0)
            {
                _logger.LogWarning(
                    $"Invalid address creation message received, discarding. message-id: {message.MessageId}");
                return;
            }

            await _serializer.CreateAsync(dto, cancellationToken);
        }
    }
}
```
