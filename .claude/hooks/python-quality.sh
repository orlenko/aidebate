#!/bin/bash
# PostToolUse hook: runs ruff check + format on .py files after Write/Edit/MultiEdit.
# Silent on success; prints ruff's output on lint findings.
# Non-blocking — we surface issues but don't fail the tool call.

set -euo pipefail

if ! command -v jq &>/dev/null; then
  exit 0
fi

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

case "$TOOL_NAME" in
  Write|Edit|MultiEdit) ;;
  *) exit 0 ;;
esac

FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE" || "$FILE" != *.py || ! -f "$FILE" ]]; then
  exit 0
fi

if [[ -z "${CLAUDE_PROJECT_DIR:-}" ]]; then
  exit 0
fi

# Prefer the project venv's ruff so we use the pinned version.
RUFF="$CLAUDE_PROJECT_DIR/.venv/bin/ruff"
if [[ ! -x "$RUFF" ]]; then
  RUFF=$(command -v ruff 2>/dev/null || true)
fi
if [[ -z "$RUFF" ]]; then
  exit 0  # ruff not installed — nothing to do.
fi

cd "$CLAUDE_PROJECT_DIR"

# Auto-format (idempotent, never destructive).
"$RUFF" format --quiet "$FILE" 2>/dev/null || true

# Lint: report any remaining issues. Don't fail the hook — Claude sees the
# output and can decide whether to act.
if ! LINT_OUT=$("$RUFF" check "$FILE" 2>&1); then
  echo "ruff findings in $FILE:" >&2
  echo "$LINT_OUT" >&2
fi

exit 0
