#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly app_service="srv-captain--geotherm-odoo"
readonly db_service="srv-captain--geotherm-odoo-db"
readonly backup_root="/root/arcigy-backups"
readonly transfer_root="${backup_root}/transfers"
readonly work_root="${backup_root}/work"
mode="run"
backup_id="${1:-}"
if [[ "$backup_id" == "--cleanup" ]]; then
  mode="cleanup"
  backup_id="${2:-}"
fi
[[ "$backup_id" =~ ^geotherm-odoo-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$ ]]
readonly mode backup_id
readonly work_dir="${work_root}/${backup_id}"
readonly archive_path="${transfer_root}/${backup_id}.tar.gz"
readonly result_path="${transfer_root}/${backup_id}.result"
readonly result_tmp_path="${transfer_root}/${backup_id}.result.tmp"
readonly lock_dir="${transfer_root}/${backup_id}.lock"

if [[ "$mode" == "cleanup" ]]; then
  for _ in $(seq 1 60); do
    if [[ ! -d "$lock_dir" ]]; then
      rm -f -- "$archive_path" "$result_path" "$result_tmp_path"
      exit 0
    fi
    sleep 2
  done
  exit 1
fi

if [[ -s "$archive_path" && -s "$result_path" ]]; then
  cat "$result_path"
  exit 0
fi

mkdir "$lock_dir"

cleanup() {
  rm -rf -- "$work_dir"
  rm -f -- "$result_tmp_path"
  rmdir "$lock_dir" 2>/dev/null || true
  if [[ "${completed:-false}" != "true" ]]; then
    rm -f -- "$archive_path" "$result_path"
  fi
}
trap cleanup EXIT

install -d -m 700 "$transfer_root" "$work_root" "$work_dir"

app_container="$(docker ps --filter "label=com.docker.swarm.service.name=${app_service}" --format '{{.ID}}' | head -n 1)"
db_container="$(docker ps --filter "label=com.docker.swarm.service.name=${db_service}" --format '{{.ID}}' | head -n 1)"
[[ -n "$app_container" && -n "$db_container" ]]

docker service inspect "$app_service" "$db_service" > "${work_dir}/service-definitions.json"
docker exec "$db_container" sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "${work_dir}/database.dump"
docker exec "$app_container" tar -C /var/lib/odoo -czf - . \
  > "${work_dir}/filestore.tar.gz"

test -s "${work_dir}/database.dump"
test -s "${work_dir}/filestore.tar.gz"
test -s "${work_dir}/service-definitions.json"
docker exec -i "$db_container" pg_restore -l < "${work_dir}/database.dump" > /dev/null
tar -tzf "${work_dir}/filestore.tar.gz" > /dev/null
sha256sum \
  "${work_dir}/database.dump" \
  "${work_dir}/filestore.tar.gz" \
  "${work_dir}/service-definitions.json" \
  > "${work_dir}/SHA256SUMS"

tar -C "$work_dir" -czf "$archive_path" \
  database.dump filestore.tar.gz service-definitions.json SHA256SUMS
test -s "$archive_path"
tar -tzf "$archive_path" > /dev/null
chmod 600 "$archive_path"

readonly archive_sha256="$(sha256sum "$archive_path" | awk '{print $1}')"
readonly archive_size_bytes="$(stat -c %s "$archive_path")"
readonly database_size_bytes="$(stat -c %s "${work_dir}/database.dump")"
readonly filestore_size_bytes="$(stat -c %s "${work_dir}/filestore.tar.gz")"
{
  printf 'backup_id=%s\n' "$backup_id"
  printf 'archive_path=%s\n' "$archive_path"
  printf 'archive_sha256=%s\n' "$archive_sha256"
  printf 'archive_size_bytes=%s\n' "$archive_size_bytes"
  printf 'database_size_bytes=%s\n' "$database_size_bytes"
  printf 'filestore_size_bytes=%s\n' "$filestore_size_bytes"
  printf 'source_app_service=%s\n' "$app_service"
  printf 'source_db_service=%s\n' "$db_service"
} > "$result_tmp_path"
mv -f -- "$result_tmp_path" "$result_path"
completed=true
cat "$result_path"
