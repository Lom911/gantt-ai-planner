#!/usr/bin/env bash
# deploy/backup.sh
#
# Nightly logical backup of the production 'planner' database. Installed
# by deploy/bootstrap.sh at /usr/local/bin/gantt-planner-backup.sh and run
# by /etc/cron.d/gantt-planner-backup at 03:15 every day. Dumps older than
# RETENTION_DAYS are pruned on every run.
#
# A failure is never silent: every run - successful or not - is logged to
# syslog under the tag 'gantt-planner-backup' (`journalctl -t
# gantt-planner-backup`) and recorded in STATUS_FILE, e.g.:
#   timestamp=2026-09-27T03:15:04Z
#   status=ok                      (or: fail)
#   exit_code=0
#   size_bytes=183422              (0 on failure)
#   file=/var/backups/gantt-planner/2026-09-27.dump
#   last_ok=2026-09-27T03:15:04Z   (kept across failures: how stale the newest good dump is)
# The app reads this file (read-only mount) for GET /api/ops/status.
#
# After a successful dump, the offsite copy runs (OFFSITE_BIN =
# deploy/offsite-backup.sh: encrypted to a public key, pushed to a private
# GitHub repository). Its outcome is logged under its own syslog tag and kept
# as offsite_* lines in STATUS_FILE; it never turns a good local backup into a
# failed one.
set -euo pipefail

APP_DIR=/opt/gantt-planner
COMPOSE_FILE="$APP_DIR/compose.prod.yml"
BACKUP_DIR=/var/backups/gantt-planner
STATUS_FILE=/var/lib/gantt-planner/backup-status
LOG_TAG=gantt-planner-backup
RETENTION_DAYS=7
OFFSITE_BIN=/usr/local/bin/gantt-planner-offsite-backup.sh
OFFSITE_TIMEOUT=900

# Dumps hold every user's data: created 0600 from the first byte.
umask 077

dump_file="$BACKUP_DIR/$(date +%F).dump"
tmp_file="${dump_file}.tmp"

# Best effort: a missing logger or an unwritable status file must not hide
# the backup's own exit code.
log() {
    priority="$1"
    shift
    logger -t "$LOG_TAG" -p "user.$priority" -- "$*" 2>/dev/null || true
}

write_status() {
    status_word="$1" exit_code="$2" size="$3" file="$4" last_ok="$5"
    (
        umask 022
        mkdir -p "$(dirname "$STATUS_FILE")"
        {
            printf 'timestamp=%s\nstatus=%s\nexit_code=%s\nsize_bytes=%s\nfile=%s\nlast_ok=%s\n' \
                "$now" "$status_word" "$exit_code" "$size" "$file" "$last_ok"
            # The offsite copy's lines (offsite-backup.sh) outlive this rewrite.
            grep '^offsite_' "$STATUS_FILE" 2>/dev/null || true
        } > "$STATUS_FILE.tmp"
        mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
    ) 2>/dev/null || echo "gantt-planner backup: could not write $STATUS_FILE" >&2
}

# Single exit path: a failed or interrupted pg_dump must not leave a partial
# file behind, and every outcome is logged and recorded.
finish() {
    status=$?
    trap - EXIT
    rm -f "$tmp_file"
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ "$status" -eq 0 ]; then
        size="$(wc -c < "$dump_file" | tr -d ' ')"
        log info "ok: wrote $dump_file ($size bytes), pruned dumps older than ${RETENTION_DAYS} days"
        write_status ok 0 "$size" "$dump_file" "$now"
        # Offsite copy of this dump: best effort and bounded; exits 0 when not configured.
        if [ -x "$OFFSITE_BIN" ]; then
            timeout -k 30 "$OFFSITE_TIMEOUT" "$OFFSITE_BIN" "$dump_file" ||
                log warning "offsite copy of $dump_file failed (exit $?); see journalctl -t gantt-planner-offsite"
        fi
    else
        prev_ok="$({ grep -E '^last_ok=' "$STATUS_FILE" 2>/dev/null || true; } | tail -n1 | cut -d= -f2-)"
        log err "FAILED (exit $status): no new dump; last good backup: ${prev_ok:-never}; see /var/log/gantt-planner-backup.log"
        write_status fail "$status" 0 "-" "$prev_ok"
        echo "gantt-planner backup: FAILED (exit $status)" >&2
    fi
    exit "$status"
}
trap finish EXIT

mkdir -p "$BACKUP_DIR"

docker compose -f "$COMPOSE_FILE" exec -T db pg_dump -U planner_owner -Fc planner > "$tmp_file"
# A dump that "succeeded" but wrote nothing is a failure too.
[ -s "$tmp_file" ]
chmod 0600 "$tmp_file"
mv "$tmp_file" "$dump_file"

find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -mtime "+${RETENTION_DAYS}" -delete
# Leftovers of runs killed hard enough to skip the trap (SIGKILL, power loss);
# older than an hour, so a dump still being written is never touched.
find "$BACKUP_DIR" -maxdepth 1 -name '*.dump.tmp' -mmin +60 -delete

echo "gantt-planner backup: wrote $dump_file, pruned dumps older than ${RETENTION_DAYS} days"
