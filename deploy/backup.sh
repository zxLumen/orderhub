#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/var/lib/orderhub}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/orderhub}"
KEEP="${KEEP:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"

install -d -o orderhub -g orderhub -m 0750 "$BACKUP_DIR"
sqlite3 "$DATA_DIR/orderhub.db" ".backup '$BACKUP_DIR/orderhub-$STAMP.db'"
gzip -f "$BACKUP_DIR/orderhub-$STAMP.db"
find "$BACKUP_DIR" -type f -name 'orderhub-*.db.gz' -mtime +"$KEEP" -delete
echo "backup: $BACKUP_DIR/orderhub-$STAMP.db.gz"
