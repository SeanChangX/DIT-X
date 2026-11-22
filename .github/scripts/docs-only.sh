#!/bin/bash
# Exit 0 when every path changed between BASE and HEAD is documentation (docs/, mkdocs.yml,
# *.md), 1 otherwise. An unknown BASE (new branch, force push) counts as "not docs-only", so
# callers fall back to the full pipeline.
#   .github/scripts/docs-only.sh BASE HEAD
set -uo pipefail
base=${1:-} head=${2:-HEAD}
git cat-file -e "$base^{commit}" 2>/dev/null || exit 1
changed=$(git diff --name-only "$base" "$head") || exit 1
[[ -n "$changed" ]] || exit 1
! grep -qvE '^(docs/|mkdocs\.yml$)|\.md$' <<<"$changed"
