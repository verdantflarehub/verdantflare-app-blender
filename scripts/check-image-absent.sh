#!/usr/bin/env bash
set -euo pipefail
: "${IMAGE:?IMAGE is required}"
inspection=$(mktemp)
trap 'rm -f "$inspection"' EXIT
if docker buildx imagetools inspect "$IMAGE" > "$inspection" 2>&1; then
  echo 'Refusing to overwrite an existing release image' >&2
  exit 1
fi
# Authentication, connectivity and repository errors must fail closed.
# Only an explicit missing-manifest response permits publishing.
if ! grep -Eqi 'manifest unknown|MANIFEST_UNKNOWN|manifest .*: not found' "$inspection" && ! grep -Fq "${IMAGE}: not found" "$inspection"; then
  echo 'Registry preflight failed; image absence was not established' >&2
  exit 1
fi
