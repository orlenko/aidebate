#!/bin/bash
# PostToolUse hook: stamps a marker file when /hostile-eyes completes.
# The marker includes the current HEAD SHA so the pre-PR hook can detect
# if new commits were added after the review.

set -euo pipefail

if ! command -v jq &>/dev/null; then
  exit 0
fi

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Only act on Skill tool invocations.
if [[ "$TOOL_NAME" != "Skill" ]]; then
  exit 0
fi

SKILL=$(echo "$INPUT" | jq -r '.tool_input.skill // empty')
if [[ "$SKILL" != "hostile-eyes" ]]; then
  exit 0
fi

# Only skip stamping on tool-level failures, not on review findings
# (an adversarial review naturally mentions "errors" and "failures").
TOOL_OUTPUT=$(echo "$INPUT" | jq -r '.tool_output // empty')
if [[ -z "$TOOL_OUTPUT" ]]; then
  exit 0
fi
if echo "$TOOL_OUTPUT" | grep -qi 'skill not found\|skill invocation failed\|skill execution error'; then
  exit 0
fi

if [[ -z "${CLAUDE_PROJECT_DIR:-}" ]]; then
  exit 0
fi

BRANCH=$(git -C "$CLAUDE_PROJECT_DIR" branch --show-current 2>/dev/null || echo "unknown")
MARKER="$CLAUDE_PROJECT_DIR/.claude/.hostile-eyes-done--${BRANCH//\//_}"

git -C "$CLAUDE_PROJECT_DIR" rev-parse HEAD > "$MARKER"

exit 0
