#!/usr/bin/env bash
# Check that every non-merge commit in BASE..HEAD is signed off by its author (DCO, ADR-012).
#
#   scripts/check-dco.sh <base> <head>
set -euo pipefail

base="${1:?usage: check-dco.sh <base> <head>}"
head="${2:?usage: check-dco.sh <base> <head>}"
status=0

for commit in $(git rev-list --no-merges "$base..$head"); do
  author="$(git show -s --format='%an <%ae>' "$commit")"
  if ! git show -s --format=%B "$commit" | grep -qixF "Signed-off-by: $author"; then
    echo "::error::$(git show -s --format='%h %s' "$commit") has no 'Signed-off-by: $author'"
    status=1
  fi
done

if [ "$status" -ne 0 ]; then
  echo "Sign off with 'git commit -s', or fix a branch with 'git rebase --signoff $base'."
fi
exit "$status"
