#!/usr/bin/env bash
# deploy/tests/test_bootstrap_backup_timer.sh
#
# Exercises bootstrap.sh's step_backup_timer (sourced, so main never runs)
# with SYSTEMD_DIR and CRON_FILE in a temp directory. 'systemctl' is
# stubbed via PATH; install's -o/-g are neutralised so this runs
# unprivileged. Nothing here touches a real server.
#
# Usage: bash deploy/tests/test_bootstrap_backup_timer.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

# shellcheck source=deploy/bootstrap.sh
source "$script_dir/../bootstrap.sh"
set -euo pipefail

SYSTEMD_DIR="$work/systemd"
CRON_FILE="$work/cron.d/gantt-planner-backup"
stub_dir="$work/bin"
mkdir -p "$SYSTEMD_DIR" "$stub_dir" "$(dirname "$CRON_FILE")"

# Unprivileged stand-in: ownership is not what is under test here.
install() {
    local args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -o | -g) shift 2 ;;
            *) args+=("$1") && shift ;;
        esac
    done
    command install "${args[@]}"
}

cat > "$stub_dir/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$SYSTEMCTL_LOG"
STUB
chmod +x "$stub_dir/systemctl"
export SYSTEMCTL_LOG="$work/systemctl.log"

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
        sed 's/^/    out: /' "$work/out.log"
        fail=$((fail + 1))
    fi
}

run_step() {
    : > "$SYSTEMCTL_LOG"
    (PATH="$stub_dir:$PATH" step_backup_timer) > "$work/out.log" 2>&1
}

# --- a host upgraded from the cron version: units installed, timer enabled, cron file gone
echo '15 3 * * * root /usr/local/bin/gantt-planner-backup.sh' > "$CRON_FILE"
run_step
for unit in gantt-planner-backup.service gantt-planner-backup.timer; do
    check "$unit installed" -f "$SYSTEMD_DIR/$unit"
    check "$unit is the repo's copy" "$(cmp -s "$script_dir/../systemd/$unit" "$SYSTEMD_DIR/$unit" && echo same)" = same
done
check "daemon-reload before enabling" "$(head -n1 "$SYSTEMCTL_LOG")" = "daemon-reload"
check "timer enabled and started" "$(grep -c '^enable --now gantt-planner-backup.timer$' "$SYSTEMCTL_LOG")" -eq 1
check "old cron file removed" ! -e "$CRON_FILE"
check "removal reported" "$(grep -c 'removed the old' "$work/out.log")" -eq 1

# --- re-run (idempotent): same result, nothing to remove
run_step
check "re-run: timer enabled again" "$(grep -c '^enable --now gantt-planner-backup.timer$' "$SYSTEMCTL_LOG")" -eq 1
check "re-run: no cron file to remove" "$(grep -c 'removed the old' "$work/out.log")" -eq 0

# --- the units themselves
service="$script_dir/../systemd/gantt-planner-backup.service"
timer="$script_dir/../systemd/gantt-planner-backup.timer"
check "service runs the installed backup script" "$(grep -c "^ExecStart=$BACKUP_BIN$" "$service")" -eq 1
check "service is oneshot" "$(grep -c '^Type=oneshot$' "$service")" -eq 1
check "timer fires at 03:15" "$(grep -c '^OnCalendar=\*-\*-\* 03:15:00$' "$timer")" -eq 1
check "timer catches up after downtime" "$(grep -c '^Persistent=true$' "$timer")" -eq 1

echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
