#!/usr/bin/env bash

# End-to-end smoke test: build a fake C# project, point the engine's marketplace
# fallback scan at this repo, fire a PreToolUse event, and assert the expected
# rules land in the injected context. Proves the published tree actually works.

# -e  Exit immediately if a command exits with a non-zero status.
set -e

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/project/src/Consumers"
touch "$TMP/project/Test.sln"
printf 'using DotNetCore.CAP;\nnamespace X;\npublic class FooConsumer : ICapSubscribe {}\n' \
  > "$TMP/project/src/Consumers/FooConsumer.cs"

# Run the engine from a copy so the smoke test never writes .state/ into the repo.
cp -R plugins/conditional-rules-plugin "$TMP/engine"

export CLAUDE_PLUGIN_ROOT="$TMP/engine"
export CLAUDE_PROJECT_DIR="$TMP/project"
export CLAUDE_INSTALLED_PLUGINS_FILE="$TMP/does-not-exist.json"  # force the marketplace fallback scan
export CLAUDE_MARKETPLACE_DIR="$PWD"                             # scanned recursively for rules.json

OUTPUT=$(python3 "$CLAUDE_PLUGIN_ROOT/hooks/conditional_rules/conditional_rules_pre.py" <<EOF
{
  "session_id": "ci-smoke",
  "hook_event_name": "PreToolUse",
  "tool_name": "Edit",
  "tool_input": { "file_path": "$CLAUDE_PROJECT_DIR/src/Consumers/FooConsumer.cs" },
  "cwd": "$CLAUDE_PROJECT_DIR"
}
EOF
)

echo "$OUTPUT" | python3 -m json.tool > /dev/null

if ! echo "$OUTPUT" | grep -q '<!-- Conditional rules active ('; then
  echo "FAIL: additionalContext does not open with the injection manifest" >&2
  exit 1
fi

for rule in main-rules messaging-cap-consumers; do
  if ! echo "$OUTPUT" | grep -q "## Rule: $rule"; then
    echo "FAIL: rule '$rule' was not injected" >&2
    exit 1
  fi
done

# Reactive rule: a just-written controller with an unversioned route and a flat
# namespace must trigger the PostToolUse content rule.
mkdir -p "$CLAUDE_PROJECT_DIR/src/Controllers/V1"
printf 'namespace DotNetTemplate.Controllers;\n[Route("addresses")]\npublic class AddressesController { }\n' \
  > "$CLAUDE_PROJECT_DIR/src/Controllers/V1/AddressesController.cs"

OUTPUT_POST=$(python3 "$CLAUDE_PLUGIN_ROOT/hooks/conditional_rules/conditional_rules_post.py" <<EOF
{
  "session_id": "ci-smoke",
  "hook_event_name": "PostToolUse",
  "tool_name": "Write",
  "tool_input": { "file_path": "$CLAUDE_PROJECT_DIR/src/Controllers/V1/AddressesController.cs" },
  "cwd": "$CLAUDE_PROJECT_DIR"
}
EOF
)

echo "$OUTPUT_POST" | python3 -m json.tool > /dev/null

if ! echo "$OUTPUT_POST" | grep -q "## Rule: csharp-invalid-api-versioning"; then
  echo "FAIL: reactive rule 'csharp-invalid-api-versioning' was not injected" >&2
  exit 1
fi

echo "Smoke test OK: expected rules injected (PreToolUse teaching + PostToolUse reactive)."
