#!/usr/bin/env bash
# deploy/offsite-backup.sh [DUMP]
#
# Offsite copy of a nightly dump, encrypted to a public key only. Installed by
# deploy/bootstrap.sh as /usr/local/bin/gantt-planner-offsite-backup.sh and run
# by deploy/backup.sh right after every successful dump (DUMP = that dump;
# without an argument: the `file=` of the last successful run in STATUS_FILE).
#
# 1. Encrypts DUMP with `openssl cms -encrypt` (AES-256-CBC, RSA key transport)
#    to the X.509 certificate $CONF_DIR/backup-recipient.pem. The matching
#    private key never lives on this server: neither the server nor the backup
#    repository can read the copies.
# 2. Pushes it as dumps/<dump name>.cms to a PRIVATE GitHub repository over SSH
#    with a write deploy key ($CONF_DIR/backup-deploy-key), github.com's host
#    key pinned in $CONF_DIR/github_known_hosts. Repository URL and branch come
#    from $CONF_DIR/offsite.conf (deploy/offsite/offsite.conf.example).
# 3. Keeps the newest OFFSITE_KEEP (default 30) dumps/*.cms in the tree, by
#    name (= date); older ones remain in the git history.
# 4. Logs to syslog (`journalctl -t gantt-planner-offsite`) and records
#    offsite_status / offsite_last_ok / offsite_file in STATUS_FILE (the other
#    lines there belong to backup.sh and are kept).
#
# Not configured (offsite.conf, the certificate or the deploy key missing):
# logs "offsite not configured" and exits 0 - the local backup still counts.
# Any other failure exits non-zero with offsite_status=fail.
# The weekly restore check: deploy/offsite/restore-check.yml.
set -euo pipefail

CONF_DIR=/etc/gantt-planner
CONF_FILE="$CONF_DIR/offsite.conf"
RECIPIENT_CERT="$CONF_DIR/backup-recipient.pem"
DEPLOY_KEY="$CONF_DIR/backup-deploy-key"
KNOWN_HOSTS="$CONF_DIR/github_known_hosts"
STATUS_FILE=/var/lib/gantt-planner/backup-status
LOG_TAG=gantt-planner-offsite
DEFAULT_KEEP=30
# GitHub rejects files over 100 MB.
MAX_BYTES=$((95 * 1024 * 1024))
REPO_RE='^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$'
BRANCH_RE='^[A-Za-z0-9][A-Za-z0-9._/-]*$'

umask 077

# Best effort, like backup.sh: a missing logger must not change the outcome.
log() {
    priority="$1"
    shift
    logger -t "$LOG_TAG" -p "user.$priority" -- "$*" 2>/dev/null || true
    echo "gantt-planner offsite: $*"
}

status_field() { { grep -E "^$1=" "$STATUS_FILE" 2>/dev/null || true; } | tail -n1 | cut -d= -f2-; }

# set_status <status> <last_ok> <file>: rewrites the offsite_* lines of STATUS_FILE.
set_status() {
    (
        umask 022
        mkdir -p "$(dirname "$STATUS_FILE")"
        {
            grep -v '^offsite_' "$STATUS_FILE" 2>/dev/null || true
            printf 'offsite_status=%s\noffsite_last_ok=%s\noffsite_file=%s\n' "$1" "$2" "$3"
        } > "$STATUS_FILE.offsite.tmp"
        mv -f "$STATUS_FILE.offsite.tmp" "$STATUS_FILE"
    ) 2>/dev/null || echo "gantt-planner offsite: could not write $STATUS_FILE" >&2
}

conf_value() {
    { grep -E "^$1=" "$CONF_FILE" || true; } | tail -n1 | cut -d= -f2- | tr -d " \t\r\"'"
}

prev_ok="$(status_field offsite_last_ok)"
prev_file="$(status_field offsite_file)"

missing=()
for f in "$CONF_FILE" "$RECIPIENT_CERT" "$DEPLOY_KEY"; do
    [ -r "$f" ] || missing+=("$f")
done
if [ "${#missing[@]}" -gt 0 ]; then
    log notice "offsite not configured (missing: ${missing[*]}); local backup only - see deploy/offsite/offsite.conf.example"
    set_status not_configured "$prev_ok" "$prev_file"
    exit 0
fi

work=""
dump_name="?"
done_ok=0
finish() {
    status=$?
    trap - EXIT
    [ -z "$work" ] || { cd / && rm -rf "$work"; }
    if [ "$done_ok" -ne 1 ]; then
        [ "$status" -ne 0 ] || status=1
        log err "FAILED (exit $status): $dump_name was not copied offsite; last offsite copy: ${prev_ok:-never}"
        set_status fail "$prev_ok" "$prev_file"
    fi
    exit "$status"
}
trap finish EXIT

fail() {
    echo "gantt-planner offsite: $*" >&2
    exit 1
}

[ -r "$KNOWN_HOSTS" ] || fail "$KNOWN_HOSTS (pinned github.com host key) is missing - re-run deploy/bootstrap.sh"
repo="$(conf_value OFFSITE_REPO)"
branch="$(conf_value OFFSITE_BRANCH)"
branch="${branch:-main}"
keep="$(conf_value OFFSITE_KEEP)"
keep="${keep:-$DEFAULT_KEEP}"
[[ "$repo" =~ $REPO_RE ]] || fail "OFFSITE_REPO '$repo' in $CONF_FILE is not git@github.com:<owner>/<repo>.git"
[[ "$branch" =~ $BRANCH_RE ]] || fail "OFFSITE_BRANCH '$branch' in $CONF_FILE is not a plain branch name"
[[ "$keep" =~ ^[1-9][0-9]*$ ]] || fail "OFFSITE_KEEP '$keep' in $CONF_FILE is not a positive number"

if [ "$#" -gt 0 ]; then
    dump="$1"
elif [ "$(status_field status)" = ok ]; then
    dump="$(status_field file)"
else
    fail "no dump given and the last backup in $STATUS_FILE did not succeed"
fi
if [ ! -f "$dump" ] || [ ! -s "$dump" ]; then
    fail "dump '$dump' is missing or empty"
fi
dump_name="$(basename "$dump")"
[[ "$dump_name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "unexpected dump file name '$dump_name'"
dump="$(cd "$(dirname "$dump")" && pwd)/$dump_name"
target="dumps/$dump_name.cms"

work="$(mktemp -d "${TMPDIR:-/tmp}/gantt-offsite.XXXXXXXX")"
# Never run git inside whatever repository the caller's cwd happens to be in.
cd "$work"
encrypted="$work/$dump_name.cms"
openssl cms -encrypt -binary -aes-256-cbc -outform DER \
    -in "$dump" -out "$encrypted" "$RECIPIENT_CERT"
[ -s "$encrypted" ] || fail "openssl cms produced no output"
size="$(wc -c < "$encrypted" | tr -d ' ')"
[ "$size" -le "$MAX_BYTES" ] || fail "$target is $size bytes, over GitHub's file size limit"

# Only the deploy key, only github.com's pinned host key, never a prompt; no
# user/system git config (hooks, url rewrites, signing) gets involved.
export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o GlobalKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null

clone="$work/repo"
remote_status=0
git ls-remote --exit-code --heads "$repo" "$branch" >/dev/null || remote_status=$?
if [ "$remote_status" -eq 0 ]; then
    # Shallow: only the current tree (at most OFFSITE_KEEP dumps) is downloaded.
    git clone --quiet --depth 1 --branch "$branch" --single-branch "$repo" "$clone"
elif [ "$remote_status" -eq 2 ]; then
    # Empty repository or no such branch yet: the first push creates it.
    git init --quiet "$clone"
    git -C "$clone" symbolic-ref HEAD "refs/heads/$branch"
    git -C "$clone" remote add origin "$repo"
else
    fail "cannot reach $repo (git ls-remote exit $remote_status)"
fi

mkdir -p "$clone/dumps"
cp "$encrypted" "$clone/$target"
git -C "$clone" add -- "$target"
mapfile -t dumps < <(git -C "$clone" ls-files -- 'dumps/*.cms' | LC_ALL=C sort)
pruned=0
if [ "${#dumps[@]}" -gt "$keep" ]; then
    pruned=$((${#dumps[@]} - keep))
    git -C "$clone" rm --quiet -- "${dumps[@]:0:$pruned}"
fi
if ! git -C "$clone" diff --cached --quiet; then
    git -C "$clone" -c user.name=gantt-planner-offsite -c user.email=offsite@gantt-planner.invalid \
        -c commit.gpgsign=false commit --quiet -m "backup $dump_name ($size bytes, encrypted)"
    git -C "$clone" push --quiet origin "HEAD:refs/heads/$branch"
fi

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
done_ok=1
set_status ok "$now" "$target"
log info "ok: pushed $target ($size bytes) to $repo ($branch), pruned $pruned old dump(s), keeping $keep"
