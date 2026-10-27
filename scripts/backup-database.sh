#!/usr/bin/env bash
# Dump the production MySQL database to a gzipped file, atomically.
#
# Runs on the droplet (systemd timer: scripts/systemd/vm-automation-backup.*).
# A dump only counts once mysqldump has written its "Dump completed" trailer;
# anything shorter is discarded so a truncated file never looks like a backup.
#
# Env:
#   COMPOSE_DIR     checkout holding docker-compose.prod.yml (default: repo root)
#   BACKUP_DIR      destination (default: /root/backups)
#   RETENTION_DAYS  prune vmdatabase-*.sql.gz older than this (default: 14).
#                   Hand-made dumps with other names are never pruned.
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
final="$BACKUP_DIR/vmdatabase-$(date +%Y%m%d-%H%M%S).sql.gz"
tmp_sql="$(mktemp "$BACKUP_DIR/.dump.XXXXXX")"
trap 'rm -f "$tmp_sql" "$tmp_sql.gz"' EXIT

# MYSQL_PWD keeps the password off the process list. --no-tablespaces: the app
# user lacks the PROCESS privilege mysqldump 8 wants for tablespace metadata.
docker compose -f "$COMPOSE_DIR/docker-compose.prod.yml" exec -T db sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u"$MYSQL_USER" --single-transaction --no-tablespaces --routines --triggers "$MYSQL_DATABASE"' \
    > "$tmp_sql"

if ! tail -n 1 "$tmp_sql" | grep -q -- '-- Dump completed'; then
    echo "backup FAILED: dump is incomplete (no completion trailer)" >&2
    exit 1
fi

gzip -9 "$tmp_sql"
mv "$tmp_sql.gz" "$final"
echo "backup OK: $final ($(du -h "$final" | cut -f1))"

find "$BACKUP_DIR" -maxdepth 1 -name 'vmdatabase-*.sql.gz' -mtime "+$RETENTION_DAYS" -print -delete
