# Project Structure

```
src/
├── Services/  ← business logic only
│   └── AuthorizationService.cs  ← sample service
└── Sdks/  ← external service communication only
    ├── CopyTokenDelegatingHandler.cs  ← shared bearer token propagation
    ├── Identity/  ← SDK name
    │   └── IdentitySdk.cs  ← sample SDK (IIdentitySdk, IdentitySdk, IdentitySdkOptions, IdentitySdkExtensions)
    └── MailGun/  ← SDK name
        └── MailGunSdk.cs  ← sample SDK
```

## Rules for Services (`src/Services/**/*Service.cs`)

- Creating a service class is an exception. Normally, it goes directly into the action method of a controller, job, or consumer.
- Contain business rules and domain orchestration
- One file per service, suffixed `Service` (e.g. `PurchaseService.cs`, `AuthorizationService.cs`)
- Each file contains: interface, implementation, and DTOs
- May depend on SDKs and libraries
- Never contain HTTP calls, serialization, or infrastructure concerns

## Rules for SDKs (`src/Sdks/**/*Sdk.cs`)

- One folder per SDK under `src/Sdks/<SdkName>/` (e.g. `src/Sdks/Identity/`, `src/Sdks/MailGun/`). The primary file is suffixed `Sdk` (e.g. `IdentitySdk.cs`).
- No business logic — only communication concerns (HTTP, headers, retries, serialization, SOAP, etc.).
- Each primary file contains: interface, implementation, DTOs, Options class, and a static DI extension method (`Add<SdkName>`). **Start with one file; split only when it grows unwieldy (lazy splitting)**. When splitting, keep the full SDK prefix on file names for greppability (e.g. `IdentitySdkDtos.cs`, `IdentitySdkResiliencePipelines.cs`).
- Options class uses `Integrations:<SdkName>` as `SectionName` with `ValidateDataAnnotations()` and `ValidateOnStart()` for fail-fast at startup.
- DI registration lives in the SDK file as `public static IServiceCollection Add<SdkName>(this IServiceCollection services, IConfiguration configuration)`. `ConfigureSharedServices` calls it as a one-liner.
- Use typed HttpClient pattern: `AddHttpClient<IFooSdk, FooSdk>` with `AddHttpMessageHandler<CopyTokenDelegatingHandler>()` for bearer token propagation
- Resilience (circuit breaker, timeout) is **per-method**, not per-client. Each method has its own `static readonly ResiliencePipeline<HttpResponseMessage>` built with `ResiliencePipelineBuilder<HttpResponseMessage>`. This prevents one failing endpoint from tripping the circuit breaker for all endpoints on the same client.
- Circuit breaker `ShouldHandle` must be explicitly configured. The Polly v8 default only handles exceptions, not HTTP status codes. Only count 5xx as failures (not 4xx): `.HandleResult(r => (int)r.StatusCode >= 500)`. Also handle `HttpRequestException` and `TimeoutRejectedException`.
- Always use `using var` on `HttpResponseMessage` to prevent connection pool starvation.
- Always propagate `CancellationToken` through all async calls (`GetAsync`, `ReadAsStringAsync`).
- `CopyTokenDelegatingHandler` lives at `src/Sdks/` root because it's shared across all SDKs.
