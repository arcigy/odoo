#!/usr/bin/env bash
set -euo pipefail

output_path="${1:?output archive path is required}"
revision="${2:-HEAD}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$output_path" != /* ]]; then
  output_path="$repo_root/$output_path"
fi

file_list="$(mktemp)"
trap 'rm -f "$file_list"' EXIT

git -C "$repo_root" archive --format=tar -o "$output_path" "$revision"
tar -tf "$output_path" > "$file_list"

grep -Fxq 'captain-definition' "$file_list"
grep -Fxq 'addons/geotherm_chatbot/__manifest__.py' "$file_list"
grep -Fxq 'addons/geotherm_drive/__manifest__.py' "$file_list"
grep -Fxq 'addons/arcigy_saas_control_center/__manifest__.py' "$file_list"

if grep -Eq '(^|/)(\.env|[^/]*\.env)$' "$file_list"; then
  echo 'Deployment archive contains an environment file.' >&2
  exit 1
fi

printf 'archive_validation=passed files=%s revision=%s\n' \
  "$(wc -l < "$file_list" | tr -d ' ')" "$revision"
