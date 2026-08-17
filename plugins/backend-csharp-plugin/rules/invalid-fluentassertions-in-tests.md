# Invalid: FluentAssertions in tests

- Replace `.Should()` chains with xUnit native asserts: `Assert.Equal`, `Assert.True`, `Assert.False`, `Assert.Contains`, `Assert.DoesNotContain`, `Assert.Equivalent`.
- Remove the `using FluentAssertions;` import and do not add the package as a dependency.
- Convert every `.Should()` assertion in this file, not only the one just written.
