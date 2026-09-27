#!/usr/bin/env bash
# deploy/tests/test_backup.sh
#
# Exercises deploy/backup.sh against a throw-away copy whose BACKUP_DIR,
# APP_DIR and STATUS_FILE point at a temp directory. 'docker' and 'logger'
# are stubbed via PATH, so no database and no syslog is touched.
#
# Usage: bash deploy/tests/test_backup.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

backups="$work/backups"
status_file="$work/state/backup-status"
stub_dir="$work/bin"
mkdir -p "$backups" "$stub_dir"
sed -e "s|^APP_DIR=.*|APP_DIR=\"$work\"|" \
    -e "s|^BACKUP_DIR=.*|BACKUP_DIR=\"$backups\"|" \
    -e "s|^STATUS_FILE=.*|STATUS_FILE=\"$status_file\"|" \
    -e "s|^OFFSITE_BIN=.*|OFFSITE_BIN=\"$work/offsite-stub\"|" \
    "$script_dir/../backup.sh" > "$work/backup.sh"

# offsite stub (stands in for offsite-backup.sh; installed only for some runs):
# records its arguments and the backup status it saw, writes offsite_* lines like
# the real one, fails when OFFSITE_FAILS=1.
cat > "$work/offsite-stub.src" <<'STUB'
#!/usr/bin/env bash
status_file="$(dirname "$0")/state/backup-status"
echo "offsite $* [saw $(grep '^status=' "$status_file")]" >> "$OFFSITE_LOG"
[ "${OFFSITE_FAILS:-0}" = 1 ] && exit 3
{ grep -v '^offsite_' "$status_file"; printf 'offsite_status=ok\noffsite_last_ok=2026-01-01T00:00:00Z\noffsite_file=dumps/x.cms\n'; } > "$status_file.o"
mv "$status_file.o" "$status_file"
STUB
export OFFSITE_LOG="$work/offsite.log"

# docker stub: `pg_dump` writes a few bytes, then fails when DUMP_FAILS=1;
# writes nothing (and succeeds) when DUMP_EMPTY=1.
cat > "$stub_dir/docker" <<'STUB'
#!/usr/bin/env bash
[ "${DUMP_EMPTY:-0}" = 1 ] && exit 0
printf 'PGDMP partial'
[ "${DUMP_FAILS:-0}" = 1 ] && exit 1
exit 0
STUB
# logger stub: records its arguments; fails when LOGGER_FAILS=1.
cat > "$stub_dir/logger" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$LOGGER_LOG"
[ "${LOGGER_FAILS:-0}" = 1 ] && exit 1
exit 0
STUB
chmod +x "$stub_dir/docker" "$stub_dir/logger"
export LOGGER_LOG="$work/logger.log"

pass=0
fail=0
# check <desc> <test args...>
check() {
    desc="$1"
    shift
    if test "$@"; then
        echo "PASS: $desc"
        pass=$((pass + 1))
    else
        echo "FAIL: $desc"
        ls -la "$backups"
        sed 's/^/    status: /' "$status_file" 2>/dev/null || true
        sed 's/^/    logger: /' "$LOGGER_LOG" 2>/dev/null || true
        fail=$((fail + 1))
    fi
}
# run_backup [VAR=value...] - sets $status
run_backup() {
    : > "$LOGGER_LOG"
    set +e
    env "$@" PATH="$stub_dir:$PATH" bash "$work/backup.sh" >/dev/null 2>&1
    status=$?
    set -e
}
status_field() { { grep -E "^$1=" "$status_file" || true; } | tail -n1 | cut -d= -f2-; }
ts_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'

today="$(date +%F)"

run_backup DUMP_FAILS=1
check "failed pg_dump exits non-zero" "$status" -ne 0
leftover="$(find "$backups" -name '*.dump*' -print -quit)"
check "failed pg_dump leaves no .dump.tmp and no .dump" -z "$leftover"
check "failure is logged to syslog as an error" \
    "$(grep -c -- "-t gantt-planner-backup -p user.err -- FAILED" "$LOGGER_LOG")" -eq 1
check "failure is recorded in the status file" "$(status_field status)" = fail
check "status file records the exit code" "$(status_field exit_code)" -ne 0
check "status file has no last good backup yet" -z "$(status_field last_ok)"

touch -d '3 hours ago' "$backups/2000-01-01.dump.tmp"
touch -d '10 days ago' "$backups/2000-01-02.dump"
run_backup
check "successful run exits zero" "$status" -eq 0
check "successful run writes today's dump" -s "$backups/$today.dump"
check "prune removes a stray .dump.tmp from an interrupted run" \
    ! -e "$backups/2000-01-01.dump.tmp"
check "prune removes dumps past retention" ! -e "$backups/2000-01-02.dump"
check "success is logged to syslog as info" \
    "$(grep -c -- "-t gantt-planner-backup -p user.info -- ok: wrote $backups/$today.dump" "$LOGGER_LOG")" -eq 1
check "success is recorded in the status file" "$(status_field status)" = ok
check "status file records the dump size" \
    "$(status_field size_bytes)" -eq "$(wc -c < "$backups/$today.dump")"
check "status file records the dump path" "$(status_field file)" = "$backups/$today.dump"
check "status file timestamp is UTC ISO-8601" "$(status_field timestamp | grep -cE "$ts_re")" -eq 1
check "last_ok is set on success" "$(status_field last_ok)" = "$(status_field timestamp)"
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) ;; # NTFS has no real POSIX modes; CI (Linux) checks this
    *) check "dump is mode 0600" "$(stat -c %a "$backups/$today.dump")" = 600 ;;
esac
last_ok="$(status_field last_ok)"

run_backup DUMP_EMPTY=1
check "empty pg_dump output is a failure" "$status" -ne 0
check "empty pg_dump output does not replace today's good dump" -s "$backups/$today.dump"
check "failure after a success keeps last_ok" "$(status_field last_ok)" = "$last_ok"
check "failure after a success is recorded" "$(status_field status)" = fail

run_backup LOGGER_FAILS=1
check "a failing logger does not fail the backup" "$status" -eq 0
check "status file is still written when logger fails" "$(status_field status)" = ok
check "no offsite script installed: nothing else runs" ! -e "$OFFSITE_LOG"

# --- offsite copy ---------------------------------------------------------------------
cp "$work/offsite-stub.src" "$work/offsite-stub"
chmod +x "$work/offsite-stub"
: > "$OFFSITE_LOG"
run_backup
check "success runs the offsite copy once, with today's dump" \
    "$(grep -c "^offsite $backups/$today.dump " "$OFFSITE_LOG")" -eq 1
check "the offsite copy runs after status=ok is recorded" "$(grep -c '\[saw status=ok\]' "$OFFSITE_LOG")" -eq 1
check "the offsite copy's status lines are recorded" "$(status_field offsite_status)" = ok
run_backup
check "the next backup keeps the offsite lines while rewriting its own" \
    "$(status_field offsite_last_ok)$(status_field status)" = "2026-01-01T00:00:00Zok"
run_backup DUMP_FAILS=1
check "a failed dump keeps the offsite lines" "$(status_field offsite_file)" = dumps/x.cms
check "a failed dump does not run the offsite copy" "$(grep -c '^offsite ' "$OFFSITE_LOG")" -eq 2
run_backup OFFSITE_FAILS=1
check "a failed offsite copy does not fail the local backup" "$status" -eq 0
check "a failed offsite copy leaves status=ok" "$(status_field status)" = ok
check "a failed offsite copy is logged as a warning" \
    "$(grep -c -- "-t gantt-planner-backup -p user.warning -- offsite copy of $backups/$today.dump failed (exit 3)" "$LOGGER_LOG")" -eq 1

echo
echo "test_backup.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
