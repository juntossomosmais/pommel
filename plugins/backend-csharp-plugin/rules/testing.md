# Testing

## Rules

- **Use xUnit** (`[Fact]`, `[Theory]`). Never add FluentAssertions to new tests; use xUnit native asserts (`Assert.Equal`, `Assert.True`, `Assert.Contains`, `Assert.DoesNotContain`, `Assert.Equivalent`, etc.) only.
- **Every test must have `// Arrange`, `// Act`, `// Assert` comment sections**, even when one section is trivial.
- **Mocking**: Use `Moq`.
  - **Moq** for interface mocking: `new Mock<IPartnerACMEService>()`
  - **Moq.AutoMock** for auto-wiring dependencies: `var mocker = new AutoMocker()`
  - Reuse existing mocks from the test project before creating new ones.
- **Test Names**: `[MethodUnderTest]_[StateUnderTest]_[ExpectedBehavior]`.
- **One test file per class under test.** Keep all tests for a given class in a single test file. Do not split tests across multiple files. When different test groups require different setup (e.g., overriding `ConfigureTestServices`), use nested classes inside a single outer class — each nested class inherits `IntegrationTests` independently.

## Publishers

For every code where we have a publisher:

- Test if the publisher is called in the same transaction as other database operations.

## Controllers

For every controller:

- Verify permissions and authorization boundaries.
- Test with minimum body fields.
- Test with maximum body fields.
- Test every combination of filters
- Test if the user just can update or get their own data, and not other users' data.
- Test both success and failure scenarios.

## Workers

For every worker:

- Test if the body of the message is not what we expect, just log a warning.
- Test if the consumer is idempotent and can handle duplicate messages.

## FluentValidation

- Test FluentValidation class separately from controller logic and consumer logic.
- Test if the controller is using the validation correctly, in the case of success or failure.
- Test if the consumer is using the validation correctly, in the case of success or failure.