# Invalid: raw string in [CapSubscribe]

- Replace the inline string with constants: `[CapSubscribe(Topics.AddressCreated, Group = Groups.AddressCreated)]`.
- Declare them as `public const string` fields in `src/Consumers/Topics.cs` — topics as `[source-system-name-prefix].[domain].[event]`, groups as `[your-app].[domain].[event]`.
- Reuse an existing constant instead of adding a duplicate with the same value — group collisions silently steal messages between consumers.
- Check every `[CapSubscribe]` in this file, not only the one just written.
