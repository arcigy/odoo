#!/usr/bin/env bash
set -euo pipefail

runtime_config="/tmp/geotherm-odoo.conf"
cp /etc/odoo/odoo.conf "$runtime_config"
printf '\nadmin_passwd = %s\n' "${ODOO_MASTER_PASSWORD:-local-master-password}" >> "$runtime_config"

if [[ "${1:-}" == "odoo" ]]; then
  shift
fi
if [[ -n "${ODOO_DB_NAME:-}" ]]; then
  set -- --database="$ODOO_DB_NAME" "$@"
fi
exec /entrypoint.sh odoo -c "$runtime_config" "$@"
