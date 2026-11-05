#!/bin/sh
# Prepare the ERP data volume mounted at /srv/box1-erp. Run as root by #25's provisioning.
# Idempotent: it fixes ownership and modes but never deletes or replaces data.
# --init-ledger creates an empty ledger only if none exists (first provisioning only).
set -eu
ROOT=/srv/box1-erp
mountpoint -q "$ROOT" || { echo "$ROOT is not a mount point; refusing (the data volume must be mounted)" >&2; exit 2; }
install -d -m 0755 -o root -g root "$ROOT" "$ROOT/exchange"
install -d -m 0750 -o box1erp -g box1erp "$ROOT/state" "$ROOT/state/spool" "$ROOT/state/spool/archive" \
  "$ROOT/state/spool/quarantine" "$ROOT/state/backups"
# setgid: files created here belong to erpxfer, shared by the worker and the SFTP account
install -d -m 2770 -o root -g erpxfer "$ROOT/exchange/inbound" "$ROOT/exchange/outbound" "$ROOT/exchange/consumed"
if [ "${1:-}" = "--init-ledger" ] && [ ! -e "$ROOT/state/ledger.db" ]; then
  runuser -u box1erp -- env BOX1_ERP_CONFIG=/etc/box1-erp/erp.conf /usr/bin/box1-erp init
fi
