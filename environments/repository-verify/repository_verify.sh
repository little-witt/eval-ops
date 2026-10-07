#!/bin/sh
set -eu

api_version="repository.verify/v1"

if [ "${1:-}" = "--self-check" ]; then
  printf '{"api_version":"%s","ready":true}\n' "$api_version"
  exit 0
fi

: "${ACEVAL_BASE_COMMIT:?ACEVAL_BASE_COMMIT is required}"
: "${ACEVAL_HEAD_COMMIT:?ACEVAL_HEAD_COMMIT is required}"

git_workspace() {
  git -c safe.directory=/workspace -C /workspace "$@"
}

git_workspace rev-parse --git-dir >/dev/null
actual_base=$(git_workspace rev-parse "${ACEVAL_BASE_COMMIT}^{commit}")
actual_head=$(git_workspace rev-parse "${ACEVAL_HEAD_COMMIT}^{commit}")
current_head=$(git_workspace rev-parse HEAD)
git_workspace merge-base --is-ancestor "$actual_base" "$actual_head"

if [ "$current_head" != "$actual_head" ] && [ "$current_head" != "$actual_base" ]; then
  printf '{"api_version":"%s","ready":false,"error":"HEAD is neither selected base nor head"}\n' "$api_version" >&2
  exit 1
fi

# NUL-delimited names keep the evidence hash unambiguous even for unusual Git
# paths. The receipt needs identity evidence, not a mutable presentation list.
changed_files_sha256=$(
  git_workspace diff --name-only -z "${actual_base}..${actual_head}" \
    | sha256sum \
    | cut -d ' ' -f 1
)

checkout_role="base"
if [ "$current_head" = "$actual_head" ]; then
  checkout_role="head"
fi

printf '{"api_version":"%s","base_commit":"%s","head_commit":"%s","current_head":"%s","checkout_role":"%s","changed_files_sha256":"sha256:%s","ready":true}\n' \
  "$api_version" "$actual_base" "$actual_head" "$current_head" "$checkout_role" "$changed_files_sha256"
