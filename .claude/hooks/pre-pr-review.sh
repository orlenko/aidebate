#!/bin/bash
# Pre-PR hook: blocks PR creation if /hostile-eyes hasn't run on non-trivial diffs.
# Triggered by PreToolUse on Bash (gh pr create) and mcp__github__create_pull_request.
#
# Logic:
#   1. For Bash tool, only act on commands containing "gh pr create".
#   2. Check diff size against main — skip if ≤10 lines changed.
#   3. Check for marker file left by /hostile-eyes — skip if SHA matches HEAD.
#   4. Block with exit 2 + stderr message if review is missing or stale.

set -euo pipefail

if ! command -v jq &>/dev/null; then
  exit 0
fi

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

if [[ -z "${CLAUDE_PROJECT_DIR:-}" ]]; then
  exit 0
fi

# For Bash tool, only intercept "gh pr create" commands.
if [[ "$TOOL_NAME" == "Bash" ]]; then
  COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
  if [[ "$COMMAND" != *"gh pr create"* ]]; then
    exit 0
  fi
fi

cd "$CLAUDE_PROJECT_DIR"

BASE_BRANCH="main"
if ! git rev-parse --verify "$BASE_BRANCH" &>/dev/null; then
  exit 0  # no main? bail — don't block.
fi

MERGE_BASE=$(git merge-base HEAD "$BASE_BRANCH" 2>/dev/null || echo "")
if [[ -z "$MERGE_BASE" ]]; then
  exit 0
fi

TOTAL_LINES=$(git diff --numstat "$MERGE_BASE..HEAD" | awk '{sum += $1 + $2} END {print sum+0}')

if [[ "$TOTAL_LINES" -le 10 ]]; then
  exit 0  # trivial change, no review needed.
fi

BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
CURRENT_SHA=$(git rev-parse HEAD 2>/dev/null)
MARKER="$CLAUDE_PROJECT_DIR/.claude/.hostile-eyes-done--${BRANCH//\//_}"

if [[ -f "$MARKER" ]]; then
  MARKER_SHA=$(tr -d '[:space:]' < "$MARKER")
  if [[ "$MARKER_SHA" == "$CURRENT_SHA" ]]; then
    exit 0
  fi
  echo "🔍 /hostile-eyes marker is stale — new commits since the review." >&2
else
  echo "🔍 This branch has ~${TOTAL_LINES} lines changed and no /hostile-eyes review." >&2
fi

echo "Run /hostile-eyes before creating the PR — it'll clear this gate for HEAD." >&2
exit 2
