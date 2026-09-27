#!/usr/bin/env bash
# deploy/tests/test_offsite_backup.sh
#
# Exercises deploy/offsite-backup.sh against a throw-away copy whose CONF_DIR
# and STATUS_FILE point at a temp directory. Stubbed via PATH:
#   - git: a wrapper that logs every call (and the GIT_SSH_COMMAND it got),
#     rewrites the configured git@github.com URL to a local bare repository and
#     runs the real git - so clone/prune/commit/push really happen, locally;
#   - openssl: "encrypts" by base64 behind a marker (the plaintext never shows
#     up in the repository) and logs its arguments;
#   - ssh, logger: log only (ssh must never be reached).
# Nothing here touches GitHub or a real server.
#
# Usage: bash deploy/tests/test_offsite_backup.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

conf="$work/etc"
status_file="$work/state/backup-status"
stub_dir="$work/bin"
bare="$work/remote.git"
tmp_root="$work/tmp"
mkdir -p "$conf" "$stub_dir" "$work/state" "$tmp_root"
sed -e "s|^CONF_DIR=.*|CONF_DIR=\"$conf\"|" \
    -e "s|^STATUS_FILE=.*|STATUS_FILE=\"$status_file\"|" \
    "$script_dir/../offsite-backup.sh" > "$work/offsite-backup.sh"

fake_url="git@github.com:owner/planner-backups.git"
if command -v cygpath >/dev/null 2>&1; then
    bare_url="file:///$(cygpath -m "$bare")" # Git for Windows
else
    bare_url="file://$bare"
fi
real_git="$(command -v git)"
export STUB_LOG="$work/calls.log"

cat > "$stub_dir/git" <<STUB
#!/usr/bin/env bash
echo "git \$* [ssh=\${GIT_SSH_COMMAND:-}]" >> "\$STUB_LOG"
args=()
for a in "\$@"; do
    [ "\$a" = "$fake_url" ] && a="$bare_url"
    args+=("\$a")
done
if [ "\${FAIL_PUSH:-0}" = 1 ] && [[ " \$* " == *" push "* ]]; then
    echo "remote: rejected (stub)" >&2
    exit 1
fi
exec "$real_git" "\${args[@]}"
STUB
cat > "$stub_dir/openssl" <<'STUB'
#!/usr/bin/env bash
echo "openssl $*" >> "$STUB_LOG"
[ "${FAIL_OPENSSL:-0}" = 1 ] && exit 1
in="" out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -in) in="$2" && shift ;;
        -out) out="$2" && shift ;;
    esac
    shift
done
{ echo "CMS-STUB"; base64 < "$in"; } > "$out"
STUB
cat > "$stub_dir/ssh" <<'STUB'
#!/usr/bin/env bash
echo "ssh $*" >> "$STUB_LOG"
exit 255
STUB
cat > "$stub_dir/logger" <<'STUB'
#!/usr/bin/env bash
echo "logger $*" >> "$STUB_LOG"
STUB
chmod +x "$stub_dir"/*

pass=0
fail=0
check() {
    desc="$1"
    shift
    if test "$@"; then
        echo "PASS: $desc"
        pass=$((pass + 1))
    else
        echo "FAIL: $desc (test $*)"
        sed 's/^/    calls: /' "$STUB_LOG"
        sed 's/^/    out: /' "$work/out.log"
        sed 's/^/    status: /' "$status_file" 2>/dev/null || true
        fail=$((fail + 1))
    fi
}
# run_offsite [VAR=value...] [-- args] - sets $status
run_offsite() {
    local envs=() args=()
    while [ "$#" -gt 0 ] && [ "$1" != -- ]; do envs+=("$1") && shift; done
    [ "$#" -eq 0 ] || { shift && args=("$@"); }
    : > "$STUB_LOG"
    set +e
    env "${envs[@]}" TMPDIR="$tmp_root" PATH="$stub_dir:$PATH" \
        bash "$work/offsite-backup.sh" "${args[@]}" >"$work/out.log" 2>&1
    status=$?
    set -e
}
status_field() { { grep -E "^$1=" "$status_file" || true; } | tail -n1 | cut -d= -f2-; }
calls() { grep -c "^$1 " "$STUB_LOG" || true; }
remote_files() { "$real_git" -C "$bare" ls-tree -r --name-only "${1:-main}" 2>/dev/null || true; }
remote_dumps() { remote_files "$1" | grep -c '^dumps/.*\.cms$' || true; }
ts_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'

# A nightly dump and the status file backup.sh left behind.
dump="$work/backups/2026-09-20.dump"
mkdir -p "$work/backups"
printf 'PGDMP secret-plaintext-marker' > "$dump"
printf 'timestamp=2026-09-20T03:15:04Z\nstatus=ok\nexit_code=0\nsize_bytes=29\nfile=%s\nlast_ok=2026-09-20T03:15:04Z\n' \
    "$dump" > "$status_file"
"$real_git" init --quiet --bare "$bare"
"$real_git" -C "$bare" symbolic-ref HEAD refs/heads/main

# --- not configured -------------------------------------------------------------------
run_offsite -- "$dump"
check "unconfigured: exits 0 (the local backup still counts)" "$status" -eq 0
check "unconfigured: says so" "$(grep -c "offsite not configured" "$work/out.log")" -eq 1
check "unconfigured: logged to syslog under gantt-planner-offsite" \
    "$(grep -c -- "logger -t gantt-planner-offsite -p user.notice -- offsite not configured" "$STUB_LOG")" -eq 1
check "unconfigured: no git, openssl or ssh call" "$(calls git)$(calls openssl)$(calls ssh)" = 000
check "unconfigured: recorded as offsite_status=not_configured" "$(status_field offsite_status)" = not_configured
check "unconfigured: backup.sh's lines are kept" "$(status_field status)$(status_field last_ok)" = "ok2026-09-20T03:15:04Z"

# --- configured ------------------------------------------------------------------------
printf 'OFFSITE_REPO=%s\nOFFSITE_BRANCH=main\n' "$fake_url" > "$conf/offsite.conf"
printf -- '-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n' > "$conf/backup-recipient.pem"
printf 'stub private deploy key\n' > "$conf/backup-deploy-key"
cp "$script_dir/../offsite/github_known_hosts" "$conf/github_known_hosts"

run_offsite -- "$dump"
check "first push into an empty repository succeeds" "$status" -eq 0
check "the encrypted dump is in the repository" "$(remote_files | grep -cx 'dumps/2026-09-20.dump.cms')" -eq 1
check "the file in the repository is the encrypted form" \
    "$("$real_git" -C "$bare" show main:dumps/2026-09-20.dump.cms | head -n1)" = CMS-STUB
check "the plaintext never reaches the repository" \
    "$("$real_git" -C "$bare" log -p --all | grep -c secret-plaintext-marker || true)" -eq 0
check "encrypted with openssl cms to the public certificate only" \
    "$(grep -cE "^openssl cms -encrypt -binary -aes-256-cbc -outform DER -in $dump -out .*/2026-09-20\.dump\.cms $conf/backup-recipient\.pem$" "$STUB_LOG")" -eq 1
check "git uses only the deploy key and the pinned github.com host key" \
    "$(grep -c -- "ssh=ssh -i $conf/backup-deploy-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$conf/github_known_hosts " "$STUB_LOG")" -gt 0
check "every git call carries that GIT_SSH_COMMAND" \
    "$(grep -c '^git ' "$STUB_LOG")" -eq "$(grep -c 'ssh=ssh -i ' "$STUB_LOG")"
check "status: offsite_status=ok" "$(status_field offsite_status)" = ok
check "status: offsite_last_ok is UTC ISO-8601" "$(status_field offsite_last_ok | grep -cE "$ts_re")" -eq 1
check "status: offsite_file names the pushed file" "$(status_field offsite_file)" = dumps/2026-09-20.dump.cms
check "status: backup.sh's lines are kept" "$(status_field status)$(status_field file)" = "ok$dump"
check "success is logged to syslog as info" \
    "$(grep -c -- "-t gantt-planner-offsite -p user.info -- ok: pushed dumps/2026-09-20.dump.cms" "$STUB_LOG")" -eq 1
check "no temp directory left behind" "$(find "$tmp_root" -mindepth 1 | wc -l | tr -d ' ')" -eq 0
first_ok="$(status_field offsite_last_ok)"

# --- no argument: the last successful dump from the status file ------------------------
run_offsite
check "without an argument the status file's dump is used" "$status" -eq 0
check "re-pushing the same dump replaces it (still one file)" "$(remote_dumps main)" -eq 1

# --- retention -------------------------------------------------------------------------
# Seed the remote with 31 older dumps plus files that are not dumps.
seed="$work/seed"
rm -rf "$seed"
"$real_git" clone --quiet --branch main "$bare_url" "$seed"
mkdir -p "$seed/dumps" "$seed/.github/workflows"
for day in $(seq -w 1 31); do printf 'old %s' "$day" > "$seed/dumps/2026-08-$day.dump.cms"; done
echo "# backups" > "$seed/README.md"
echo "name: Restore check" > "$seed/.github/workflows/restore-check.yml"
"$real_git" -C "$seed" -c core.autocrlf=false add -A
"$real_git" -C "$seed" -c user.name=t -c user.email=t@t commit --quiet -m seed
"$real_git" -C "$seed" push --quiet origin HEAD:main

dump2="$work/backups/2026-09-21.dump"
printf 'PGDMP day two' > "$dump2"
run_offsite -- "$dump2"
check "push with 33 dumps in the tree succeeds" "$status" -eq 0
check "retention keeps exactly the newest 30 dumps" "$(remote_dumps main)" -eq 30
check "the new dump is kept" "$(remote_files | grep -cx 'dumps/2026-09-21.dump.cms')" -eq 1
check "the oldest three are pruned from the tree" \
    "$(remote_files | grep -cE '^dumps/2026-08-0[123]\.dump\.cms$' || true)" -eq 0
check "the next oldest is kept" "$(remote_files | grep -cx 'dumps/2026-08-04.dump.cms')" -eq 1
check "files that are not dumps are left alone" \
    "$(remote_files | grep -cxE 'README\.md|\.github/workflows/restore-check\.yml')" -eq 2
check "pruned dumps stay in the git history" \
    "$("$real_git" -C "$bare" log --oneline main -- dumps/2026-08-01.dump.cms | wc -l | tr -d ' ')" -ge 1

printf 'OFFSITE_REPO=%s\nOFFSITE_BRANCH=main\nOFFSITE_KEEP=3\n' "$fake_url" > "$conf/offsite.conf"
run_offsite -- "$dump2"
check "OFFSITE_KEEP=3 keeps 3 dumps" "$(remote_dumps main)" -eq 3
check "OFFSITE_KEEP=3 keeps the newest ones" "$(remote_files | grep '^dumps/' | LC_ALL=C sort | tail -n1)" = dumps/2026-09-21.dump.cms

printf 'OFFSITE_REPO="%s"\nOFFSITE_BRANCH=backups\n' "$fake_url" > "$conf/offsite.conf"
run_offsite -- "$dump2"
check "a quoted URL and a new branch work (branch created on first push)" "$status" -eq 0
check "the new branch has the dump" "$(remote_files backups | grep -cx 'dumps/2026-09-21.dump.cms')" -eq 1
printf 'OFFSITE_REPO=%s\nOFFSITE_BRANCH=main\n' "$fake_url" > "$conf/offsite.conf"
last_ok="$(status_field offsite_last_ok)"

# --- failures --------------------------------------------------------------------------
# A dump that is not in the repository yet (the stub's "ciphertext" is deterministic, so
# re-sending dump2 would have nothing to push).
dump3="$work/backups/2026-09-22.dump"
printf 'PGDMP day three' > "$dump3"
before="$("$real_git" -C "$bare" rev-parse main)"
run_offsite FAIL_PUSH=1 -- "$dump3"
check "a rejected push fails" "$status" -ne 0
check "failure is recorded as offsite_status=fail" "$(status_field offsite_status)" = fail
check "failure keeps offsite_last_ok of the last good copy" "$(status_field offsite_last_ok)" = "$last_ok"
check "failure is logged to syslog as an error" \
    "$(grep -c -- "-t gantt-planner-offsite -p user.err -- FAILED" "$STUB_LOG")" -eq 1
check "failure leaves no temp directory" "$(find "$tmp_root" -mindepth 1 | wc -l | tr -d ' ')" -eq 0

run_offsite FAIL_OPENSSL=1 -- "$dump3"
check "an encryption failure fails" "$status" -ne 0
check "an encryption failure pushes nothing (no git call at all)" "$(calls git)" -eq 0
check "the remote is unchanged after failures" "$("$real_git" -C "$bare" rev-parse main)" = "$before"

printf 'OFFSITE_REPO=https://github.com/owner/planner-backups.git\n' > "$conf/offsite.conf"
run_offsite -- "$dump2"
check "a non-SSH / non-GitHub repository URL is refused" "$status" -ne 0
check "a refused URL makes no git call" "$(calls git)" -eq 0
printf 'OFFSITE_REPO=%s\nOFFSITE_BRANCH=--upload-pack=evil\n' "$fake_url" > "$conf/offsite.conf"
run_offsite -- "$dump2"
check "an option-looking branch name is refused" "$status" -ne 0
printf 'OFFSITE_REPO=%s\nOFFSITE_BRANCH=main\n' "$fake_url" > "$conf/offsite.conf"

run_offsite -- "$work/backups/missing.dump"
check "a missing dump fails" "$status" -ne 0
: > "$work/backups/empty.dump"
run_offsite -- "$work/backups/empty.dump"
check "an empty dump fails" "$status" -ne 0

mv "$conf/github_known_hosts" "$conf/kh.bak"
run_offsite -- "$dump2"
check "configured but no pinned host key: fails (not silently skipped)" "$status" -ne 0
check "missing host key is reported" "$(grep -c "github_known_hosts (pinned github.com host key) is missing" "$work/out.log")" -eq 1
mv "$conf/kh.bak" "$conf/github_known_hosts"

# A dump path relative to the caller's cwd (the script itself works in its own temp dir).
: > "$STUB_LOG"
set +e
(cd "$work/backups" && TMPDIR="$tmp_root" PATH="$stub_dir:$PATH" bash "$work/offsite-backup.sh" 2026-09-22.dump) \
    >"$work/out.log" 2>&1
status=$?
set -e
check "a relative dump path works" "$status" -eq 0
check "the relatively named dump is pushed" "$(remote_files main | grep -cx 'dumps/2026-09-22.dump.cms')" -eq 1

check "ssh was never reached directly by the tests (URL rewritten)" "$(calls ssh)" -eq 0
check "first successful copy's timestamp was recorded" -n "$first_ok"

echo
echo "test_offsite_backup.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
