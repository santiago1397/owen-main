#!/usr/bin/env bash
# Print the wayfinder ticket board for the BulkVS+Asterisk platform map.
# Usage:  bash .scratch/bulkvs-asterisk-platform/board.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/issues"

printf '\n  %-4s %-42s %s\n' "#" "TICKET" "STATUS"
printf '  %s\n' "-----------------------------------------------------------------------------"
for f in "$DIR"/[0-9]*.md; do
  num=$(basename "$f" | cut -d- -f1)
  title=$(grep -m1 '^# ' "$f" | sed 's/^# //')
  status=$(grep -m1 '^Status:' "$f" | sed 's/^Status: *//')
  case "$status" in
    resolved*|closed*) icon="[x]" ;;
    in-progress*)      icon="[~]" ;;
    claimed*)          icon="[c]" ;;
    *)                 icon="[ ]" ;;
  esac
  # trim status to first clause for width
  short=$(printf '%s' "$status" | sed 's/ (.*//' | cut -c1-24)
  printf '  %-4s %-42s %s %s\n' "$num" "$(printf '%.42s' "$title")" "$icon" "$short"
done

echo
echo "  Legend: [x] done   [~] active   [c] claimed   [ ] open"
echo "  Full overview: open  $DIR/../map.md"
echo
