#!/usr/bin/env bash
# deploy/tests/test_planner_deploy.sh
#
# Exercises planner-deploy's lock / pull / snapshot / up / health-check /
# rollback flow against a throw-away copy of the script whose APP_DIR,
# BACKUP_DIR and LOCK_FILE point at a temp directory. 'docker', 'sleep' and
# 'timeout' are stubbed via PATH (and 'flock' too where the system has none,
# e.g. Git Bash): nothing real is pulled, dumped or started, and nothing here
# touches a real server.
#
# Usage: bash deploy/tests/test_planner_deploy.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

app_dir="$work/app"
backups="$work/backups"
stub_dir="$work/bin"
lock_file="$work/planner-deploy.lock"
mkdir -p "$app_dir" "$stub_dir"
sed -e "s|^APP_DIR=.*|APP_DIR=\"$app_dir\"|" \
    -e "s|^BACKUP_DIR=.*|BACKUP_DIR=\"$backups\"|" \
    -e "s|^LOCK_FILE=.*|LOCK_FILE=\"$lock_file\"|" \
    -e "s|^LOCK_WAIT=.*|LOCK_WAIT=1|" \
    -e "s|^HEALTH_TIMEOUT=.*|HEALTH_TIMEOUT=3|" \
    "$script_dir/../planner-deploy" > "$work/planner-deploy"
chmod +x "$work/planner-deploy"
: > "$app_dir/compose.prod.yml"

# docker stub: logs each call with the tag Compose would use (IMAGE_TAG from
# the environment wins over .env, as in Compose), and fails on demand:
#   FAIL_PULL=1            -> every `compose pull` fails
#   FAIL_DUMP=1            -> `exec db pg_dump` writes a partial dump, then fails
#   DUMP_EMPTY=1           -> `exec db pg_dump` succeeds but writes nothing
#   FAIL_UP=<tag>|all      -> `compose up` fails while that tag is in effect / always
#   UNHEALTHY=<tag>|all    -> the in-container /healthz probe fails for that tag / always
cat > "$stub_dir/docker" <<'STUB'
#!/usr/bin/env bash
env_tag="$({ grep -E '^IMAGE_TAG=' .env || true; } | tail -n1 | cut -d= -f2-)"
tag="${IMAGE_TAG:-$env_tag}"
echo "$* [tag=$tag]" >> "$STUB_LOG"
case " $* " in
    *" pull "*) [ "${FAIL_PULL:-0}" = 1 ] && exit 1 ;;
    *" exec -T db pg_dump "*)
        [ "${DUMP_EMPTY:-0}" = 1 ] && exit 0
        printf 'PGDMP fake dump'
        [ "${FAIL_DUMP:-0}" = 1 ] && exit 1 ;;
    *" up "*) case "${FAIL_UP:-}" in all | "$tag") exit 1 ;; esac ;;
    *" exec -T app "*) case "${UNHEALTHY:-}" in all | "$tag") exit 1 ;; esac ;;
esac
exit 0
STUB
# timeout stub: logs `TIMEOUT(<options and duration>) <command>` and runs the
# command, or - when the command line matches the regex in TIMEOUT_ON -
# pretends the deadline hit (exit 124) without running it.
cat > "$stub_dir/timeout" <<'STUB'
#!/usr/bin/env bash
opts=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        -k | -s) opts+=("$1" "$2") && shift 2 ;;
        -*) opts+=("$1") && shift ;;
        *) break ;;
    esac
done
opts+=("$1")
shift
echo "TIMEOUT(${opts[*]}) $*" >> "$STUB_LOG"
if [ -n "${TIMEOUT_ON:-}" ] && [[ "$*" =~ $TIMEOUT_ON ]]; then
    exit 124
fi
exec "$@"
STUB
printf '#!/usr/bin/env bash\nexit 0\n' > "$stub_dir/sleep"
# date stub: FIXED_DATE (e.g. 20260101T000000Z) pins the snapshot timestamp.
real_date="$(command -v date)"
cat > "$stub_dir/date" <<STUB
#!/usr/bin/env bash
if [ -n "\${FIXED_DATE:-}" ] && [ "\$*" = "-u +%Y%m%dT%H%M%SZ" ]; then
    echo "\$FIXED_DATE"
    exit 0
fi
exec "$real_date" "\$@"
STUB
chmod +x "$stub_dir/docker" "$stub_dir/timeout" "$stub_dir/sleep" "$stub_dir/date"

# flock: the real one where it exists (Linux, CI) - a second process then
# really cannot take the lock the test holds. Git Bash has none: a stub that
# fails while the marker file exists stands in for "lock is held".
lock_marker="$work/lock.held"
if command -v flock >/dev/null 2>&1; then
    real_flock=1
else
    real_flock=0
    cat > "$stub_dir/flock" <<STUB
#!/usr/bin/env bash
echo "flock \$*" >> "\$STUB_LOG"
[ -e "$lock_marker" ] && exit 1
exit 0
STUB
    chmod +x "$stub_dir/flock"
fi
hold_lock() {
    if [ "$real_flock" = 1 ]; then
        exec 8>"$lock_file"
        flock -n 8
    else
        : > "$lock_file"
        : > "$lock_marker"
    fi
}
release_lock() {
    if [ "$real_flock" = 1 ]; then
        exec 8>&-
    else
        rm -f "$lock_marker"
    fi
}

pass=0
fail=0
digest_a="sha256:$(printf '0123456789abcdef%.0s' 1 2 3 4)"
digest_b="sha256:$(printf 'fedcba9876543210%.0s' 1 2 3 4)"
prev="sha-1111111@$digest_b"
new="sha-2222222@$digest_a"
PREPARE=:

# run_case <desc> <expect_status> <expect_env_tag> <expect_log_regex|""> <reject_log_regex|""> [VAR=value...]
# Runs planner-deploy "$new" on a fresh .env (IMAGE_TAG=$prev, or no IMAGE_TAG
# line at all when $prev is empty) and an empty backup dir; $PREPARE runs
# right before the script (to seed extra state).
run_case() {
    desc="$1" expect_status="$2" expect_tag="$3" want="$4" reject="$5"
    shift 5
    {
        echo "COMPOSE_PROJECT_NAME=gantt-planner"
        [ -z "$prev" ] || echo "IMAGE_TAG=$prev"
    } > "$app_dir/.env"
    rm -rf "$backups"
    mkdir -p "$backups"
    export STUB_LOG="$work/docker.log"
    : > "$STUB_LOG"
    "$PREPARE"
    set +e
    started=$SECONDS
    env "$@" PATH="$stub_dir:$PATH" "$work/planner-deploy" "$new" >"$work/out.log" 2>&1
    status=$?
    elapsed=$((SECONDS - started))
    set -e
    got_tag="$({ grep -E '^IMAGE_TAG=' "$app_dir/.env" || true; } | tail -n1 | cut -d= -f2-)"
    problem=""
    [ "$status" -eq "$expect_status" ] || problem="exit $status, expected $expect_status"
    [ -z "$problem" ] && [ "$got_tag" != "$expect_tag" ] && problem=".env has '$got_tag', expected '$expect_tag'"
    [ -z "$problem" ] && ! grep -q "^COMPOSE_PROJECT_NAME=gantt-planner$" "$app_dir/.env" && problem=".env lost other lines"
    [ -z "$problem" ] && [ -n "$want" ] && ! grep -qE "$want" "$STUB_LOG" && problem="no call matching /$want/"
    [ -z "$problem" ] && [ -n "$reject" ] && grep -qE "$reject" "$STUB_LOG" && problem="unexpected call matching /$reject/"
    report "$desc" "$problem"
}

# report <desc> <problem|"">
report() {
    if [ -n "$2" ]; then
        echo "FAIL: $1 ($2)"
        sed 's/^/    calls: /' "$STUB_LOG"
        sed 's/^/    out: /' "$work/out.log"
        find "$backups" -mindepth 1 | sed 's/^/    backups: /'
        fail=$((fail + 1))
    else
        echo "PASS: $1"
        pass=$((pass + 1))
    fi
}

# check <desc> <test args...> - an extra assertion on the state the last run_case left.
check() {
    desc="$1"
    shift
    if test "$@"; then report "$desc" ""; else report "$desc" "check failed: test $*"; fi
}

# line number of the first call matching <regex>, or 0
first_call() { { grep -nE "$1" "$STUB_LOG" || echo 0; } | head -n1 | cut -d: -f1; }
snapshots() { find "$backups" -maxdepth 1 -name 'pre-deploy-*.dump' | wc -l | tr -d ' '; }
leftovers() { find "$backups" -maxdepth 1 \( -name '*.tmp' -o -name '.pre-deploy.tmp.*' \) | wc -l | tr -d ' '; }
out_has() { grep -c -- "$1" "$work/out.log" || true; }

pg_dump_call="exec -T db pg_dump --lock-wait-timeout=60s -U planner_owner -Fc planner \[tag=$prev\]"

run_case "healthy deploy keeps the new ref" 0 "$new" \
    "pull app migrate \[tag=$new\]" "up -d \[tag=$prev\]|--no-deps"
check "healthy deploy snapshots the database before 'up -d'" \
    "$(first_call "$pg_dump_call")" -gt 0 -a \
    "$(first_call "$pg_dump_call")" -lt "$(first_call "up -d \[tag=$new\]")"
check "healthy deploy leaves exactly one non-empty pre-deploy dump" "$(snapshots)" -eq 1
snapshot="$(find "$backups" -maxdepth 1 -name 'pre-deploy-*.dump' -print -quit)"
check "pre-deploy dump is non-empty" -s "$snapshot"
check "pre-deploy dump name carries a UTC timestamp" \
    "$(basename "$snapshot" | grep -cE '^pre-deploy-[0-9]{8}T[0-9]{6}Z\.dump$')" -eq 1
check "no temp file left behind" "$(leftovers)" -eq 0
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) ;; # NTFS has no real POSIX modes; CI (Linux) checks this
    *) check "pre-deploy dump is mode 0600" "$(stat -c %a "$snapshot")" = 600 ;;
esac
# Hard deadlines on everything that can hang.
check "pull runs under 'timeout -k 10 300'" \
    "$(first_call "^TIMEOUT\(-k 10 300\) docker compose -f .*/compose.prod.yml pull app migrate$")" -gt 0
check "pg_dump runs under 'timeout -k 10 300'" \
    "$(first_call "^TIMEOUT\(-k 10 300\) docker compose -f .*/compose.prod.yml exec -T db pg_dump --lock-wait-timeout=60s ")" -gt 0
check "'up -d' runs under 'timeout -k 10 300'" \
    "$(first_call "^TIMEOUT\(-k 10 300\) docker compose -f .*/compose.prod.yml up -d$")" -gt 0
check "health probes run under a timeout" \
    "$(first_call "^TIMEOUT\(-k 10 20\) docker compose -f .*/compose.prod.yml exec -T app python")" -gt 0
check "every docker call goes through timeout" \
    "$(grep -c '^TIMEOUT(' "$STUB_LOG")" -eq "$(grep -c ' \[tag=' "$STUB_LOG")"

run_case "failed pull leaves .env and the running stack alone" 1 "$prev" \
    "pull app migrate \[tag=$new\]" "pg_dump|up -d" FAIL_PULL=1
run_case "pull past its deadline leaves .env and the running stack alone" 1 "$prev" \
    "TIMEOUT.* pull app migrate" "pg_dump|up -d" TIMEOUT_ON=" pull "
check "pull deadline is reported" "$(out_has "did not finish within 300s; nothing changed")" -eq 1

run_case "failed snapshot aborts before .env or the stack change" 1 "$prev" \
    "$pg_dump_call" "up -d" FAIL_DUMP=1
check "failed snapshot leaves no dump and no temp file" "$(snapshots)$(leftovers)" = 00

run_case "empty snapshot aborts before .env or the stack change" 1 "$prev" \
    "$pg_dump_call" "up -d" DUMP_EMPTY=1
check "empty snapshot leaves no dump and no temp file" "$(snapshots)$(leftovers)" = 00

run_case "snapshot past its deadline aborts before .env or the stack change" 1 "$prev" \
    "TIMEOUT.* pg_dump " "up -d" TIMEOUT_ON=" pg_dump "
check "snapshot deadline is reported" "$(out_has "pg_dump did not finish within 300s")" -eq 1
check "timed-out snapshot leaves no dump and no temp file" "$(snapshots)$(leftovers)" = 00

# Rollback restarts only `app` on the previous ref: never a plain `up -d`,
# which would re-run `migrate` with the previous image.
run_case "failed up -d restores the previous ref and restarts app only" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "up -d \[tag=$prev\]" FAIL_UP="$new"
run_case "'up -d' past its deadline rolls back too" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "up -d \[tag=$prev\]" TIMEOUT_ON=" up -d$"
check "'up -d' deadline is reported" "$(out_has "'up -d' with '$new' did not finish within 300s")" -eq 1
check "rollback 'up -d --no-deps app' has a deadline too" \
    "$(first_call "^TIMEOUT\(-k 10 300\) docker compose -f .*/compose.prod.yml up -d --no-deps app$")" -gt 0
run_case "failed health check restores the previous ref and restarts app only" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "up -d \[tag=$prev\]" UNHEALTHY="$new"
check "rollback reports the previous ref healthy" \
    "$(out_has "rolled back, '$prev' is healthy")" -eq 1
check "rollback names the pre-deploy snapshot" \
    "$(out_has "pre-deploy snapshot of the database: $backups/pre-deploy-")" -eq 1
run_case "rollback that is unhealthy too asks for manual intervention" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "up -d \[tag=$prev\]" UNHEALTHY=all
check "unhealthy rollback is reported" \
    "$(out_has "not healthy either - manual intervention needed")" -eq 1
run_case "failed rollback up -d still leaves .env on the previous ref" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "" FAIL_UP=all
check "failed rollback is reported" \
    "$(out_has "rollback 'up -d --no-deps app' failed - manual intervention needed")" -eq 1

# Pre-deploy dumps past retention are pruned; nightly dumps are backup.sh's business.
seed_old_dumps() {
    touch -d '10 days ago' "$backups/pre-deploy-20000101T000000Z.dump" "$backups/2000-01-02.dump"
    touch -d '2 days ago' "$backups/pre-deploy-20000103T000000Z.dump"
    touch -d '3 hours ago' "$backups/.pre-deploy.tmp.AbCdEf12"
    touch "$backups/.pre-deploy.tmp.fresh123"
}
PREPARE=seed_old_dumps
run_case "healthy deploy with old snapshots around" 0 "$new" "up -d \[tag=$new\]" ""
PREPARE=:
check "pre-deploy dumps past retention are pruned" ! -e "$backups/pre-deploy-20000101T000000Z.dump"
check "recent pre-deploy dumps are kept" -e "$backups/pre-deploy-20000103T000000Z.dump"
check "nightly dumps are left alone" -e "$backups/2000-01-02.dump"
check "stale temp file of a killed run is removed" ! -e "$backups/.pre-deploy.tmp.AbCdEf12"
check "a fresh temp file (maybe another run's) is left alone" -e "$backups/.pre-deploy.tmp.fresh123"

# Two snapshots in the same second never overwrite each other.
seed_same_second() { printf 'older snapshot' > "$backups/pre-deploy-20260101T000000Z.dump"; }
PREPARE=seed_same_second
run_case "snapshot name already taken (same second)" 0 "$new" "up -d \[tag=$new\]" "" \
    FIXED_DATE=20260101T000000Z
PREPARE=:
check "the earlier snapshot is not overwritten" \
    "$(cat "$backups/pre-deploy-20260101T000000Z.dump")" = "older snapshot"
check "the new snapshot gets a unique name next to it" \
    "$(find "$backups" -maxdepth 1 -name 'pre-deploy-20260101T000000Z-*.dump' | wc -l | tr -d ' ')" -eq 1

# Every file secret of compose.prod.yml must exist as a regular file before anything
# happens - otherwise Docker would create a directory in its place.
seed_secrets() {
    printf '%s\n' 'secrets:' '  db_app_password:' '    file: ./secrets/db_app_password' \
        '  ops_token:' '    file: ./secrets/ops_token' > "$app_dir/compose.prod.yml"
    rm -rf "$app_dir/secrets"
    mkdir -p "$app_dir/secrets"
    : > "$app_dir/secrets/db_app_password"
}
PREPARE=seed_secrets
run_case "a missing secret file aborts before any docker call" 1 "$prev" "" "docker|compose|TIMEOUT"
check "missing secret file is reported" "$(out_has "secret file $app_dir/secrets/ops_token is missing")" -eq 1
seed_secret_dir() { seed_secrets && mkdir "$app_dir/secrets/ops_token"; }
PREPARE=seed_secret_dir
run_case "a directory in place of a secret file aborts before any docker call" 1 "$prev" "" "docker|compose|TIMEOUT"
seed_all_secrets() { seed_secrets && : > "$app_dir/secrets/ops_token"; }
PREPARE=seed_all_secrets
run_case "all secret files present: deploy proceeds" 0 "$new" "up -d \[tag=$new\]" ""
PREPARE=:
: > "$app_dir/compose.prod.yml"

# A held lock: the second deploy gives up after LOCK_WAIT without reading
# .env, calling docker or writing a snapshot.
PREPARE=hold_lock
run_case "a deploy while another holds the lock fails fast and touches nothing" 1 "$prev" \
    "" "docker|compose|TIMEOUT"
PREPARE=:
release_lock
check "held lock is reported" "$(out_has "another deploy is running")" -eq 1
check "held lock: no snapshot, no temp file" "$(snapshots)$(leftovers)" = 00
check "held lock: gave up within a few seconds" "$elapsed" -le 5
run_case "the lock is free again once the other deploy is done" 0 "$new" "up -d \[tag=$new\]" ""

case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) ;; # `ln -s` copies on Git Bash by default
    *)
        plant_symlink() { ln -sf "$work/elsewhere" "$lock_file"; }
        PREPARE=plant_symlink
        run_case "a symlink planted as the lock file is refused" 1 "$prev" "" "."
        PREPARE=:
        check "planted lock symlink is reported" "$(out_has "is a symlink or not owned by")" -eq 1
        check "the symlink target was not created" ! -e "$work/elsewhere"
        rm -f "$lock_file"
        ;;
esac

# The rollback target read from .env must itself be a digest-pinned ref.
for bad_prev in latest "sha-1111111" "sha-1111111@$digest_b sha-3333333@$digest_b" \
    "sha-1111111@$digest_b;reboot" "sha-XYZ1234@$digest_b" ""; do
    prev="$bad_prev"
    run_case "invalid or missing current IMAGE_TAG '$bad_prev' aborts before any docker call" \
        1 "$bad_prev" "" "docker|compose|TIMEOUT"
done
check "missing IMAGE_TAG is reported" "$(out_has "could not read current IMAGE_TAG")" -eq 1
prev="sha-1111111@$digest_b"

# Digest-pinned refs (what CD sends): written to .env verbatim - the '@' and ':'
# must survive set_tag's sed in both directions (deploy and rollback).
new="sha-0123456789abcdef0123456789abcdef01234567@$digest_a"
run_case "healthy deploy of a full-length digest-pinned ref writes it to .env verbatim" 0 "$new" \
    "pull app migrate \[tag=$new\]" ""
run_case "failed health check restores the digest-pinned previous ref verbatim" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "" UNHEALTHY="$new"

# Malformed refs - bare tags included - never reach docker and leave .env alone.
for bad in "sha-2222222" "sha-2222222@${digest_a%?}" "sha-2222222@sha512:${digest_a#sha256:}" \
    "sha-2222222@sha256:$(printf 'ABCDEF0123456789%.0s' 1 2 3 4)" "sha-2222222@$digest_a@$digest_b" \
    "latest" "latest@$digest_a"; do
    new="$bad"
    run_case "malformed ref '$bad' rejected before any docker call" 1 "$prev" "" "."
done

echo
echo "test_planner_deploy.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
