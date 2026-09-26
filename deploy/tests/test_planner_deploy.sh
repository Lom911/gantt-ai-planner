#!/usr/bin/env bash
# deploy/tests/test_planner_deploy.sh
#
# Exercises planner-deploy's pull / snapshot / up / health-check / rollback
# flow against a throw-away copy of the script whose APP_DIR and BACKUP_DIR
# point at a temp directory. 'docker' and 'sleep' are stubbed via PATH:
# nothing real is pulled, dumped or started, and nothing here touches a real
# server.
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
mkdir -p "$app_dir" "$stub_dir"
sed -e "s|^APP_DIR=.*|APP_DIR=\"$app_dir\"|" \
    -e "s|^BACKUP_DIR=.*|BACKUP_DIR=\"$backups\"|" \
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
printf '#!/usr/bin/env bash\nexit 0\n' > "$stub_dir/sleep"
chmod +x "$stub_dir/docker" "$stub_dir/sleep"

pass=0
fail=0
prev=sha-1111111
new=sha-2222222
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
    env "$@" PATH="$stub_dir:$PATH" "$work/planner-deploy" "$new" >"$work/out.log" 2>&1
    status=$?
    set -e
    got_tag="$({ grep -E '^IMAGE_TAG=' "$app_dir/.env" || true; } | tail -n1 | cut -d= -f2-)"
    problem=""
    [ "$status" -eq "$expect_status" ] || problem="exit $status, expected $expect_status"
    [ -z "$problem" ] && [ "$got_tag" != "$expect_tag" ] && problem=".env has '$got_tag', expected '$expect_tag'"
    [ -z "$problem" ] && ! grep -q "^COMPOSE_PROJECT_NAME=gantt-planner$" "$app_dir/.env" && problem=".env lost other lines"
    [ -z "$problem" ] && [ -n "$want" ] && ! grep -qE "$want" "$STUB_LOG" && problem="no docker call matching /$want/"
    [ -z "$problem" ] && [ -n "$reject" ] && grep -qE "$reject" "$STUB_LOG" && problem="unexpected docker call matching /$reject/"
    report "$desc" "$problem"
}

# report <desc> <problem|"">
report() {
    if [ -n "$2" ]; then
        echo "FAIL: $1 ($2)"
        sed 's/^/    docker /' "$STUB_LOG"
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

# line number of the first docker call matching <regex>, or 0
first_call() { { grep -nE "$1" "$STUB_LOG" || echo 0; } | head -n1 | cut -d: -f1; }
snapshots() { find "$backups" -maxdepth 1 -name 'pre-deploy-*.dump' | wc -l | tr -d ' '; }
leftovers() { find "$backups" -maxdepth 1 -name '*.tmp' | wc -l | tr -d ' '; }

pg_dump_call="exec -T db pg_dump -U planner_owner -Fc planner \[tag=$prev\]"

run_case "healthy deploy keeps the new tag" 0 "$new" \
    "pull app migrate \[tag=$new\]" "up -d \[tag=$prev\]|--no-deps"
check "healthy deploy snapshots the database before 'up -d'" \
    "$(first_call "$pg_dump_call")" -gt 0 -a \
    "$(first_call "$pg_dump_call")" -lt "$(first_call "up -d \[tag=$new\]")"
check "healthy deploy leaves exactly one non-empty pre-deploy dump" "$(snapshots)" -eq 1
snapshot="$(find "$backups" -maxdepth 1 -name 'pre-deploy-*.dump' -print -quit)"
check "pre-deploy dump is non-empty" -s "$snapshot"
check "pre-deploy dump name carries a UTC timestamp" \
    "$(basename "$snapshot" | grep -cE '^pre-deploy-[0-9]{8}T[0-9]{6}Z\.dump$')" -eq 1
check "no .tmp left behind" "$(leftovers)" -eq 0
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) ;; # NTFS has no real POSIX modes; CI (Linux) checks this
    *) check "pre-deploy dump is mode 0600" "$(stat -c %a "$snapshot")" = 600 ;;
esac

run_case "failed pull leaves .env and the running stack alone" 1 "$prev" \
    "pull app migrate \[tag=$new\]" "pg_dump|up -d" FAIL_PULL=1

run_case "failed snapshot aborts before .env or the stack change" 1 "$prev" \
    "$pg_dump_call" "up -d" FAIL_DUMP=1
check "failed snapshot leaves no dump and no .tmp" "$(snapshots)$(leftovers)" = 00

run_case "empty snapshot aborts before .env or the stack change" 1 "$prev" \
    "$pg_dump_call" "up -d" DUMP_EMPTY=1
check "empty snapshot leaves no dump and no .tmp" "$(snapshots)$(leftovers)" = 00

# Rollback restarts only `app` on the previous tag: never a plain `up -d`,
# which would re-run `migrate` with the previous image.
run_case "failed up -d restores the previous tag and restarts app only" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "up -d \[tag=$prev\]" FAIL_UP="$new"
run_case "failed health check restores the previous tag and restarts app only" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "up -d \[tag=$prev\]" UNHEALTHY="$new"
check "rollback reports the previous tag healthy" \
    "$(grep -c "rolled back, '$prev' is healthy" "$work/out.log")" -eq 1
check "rollback names the pre-deploy snapshot" \
    "$(grep -c "pre-deploy snapshot of the database: $backups/pre-deploy-" "$work/out.log")" -eq 1
run_case "rollback that is unhealthy too asks for manual intervention" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "up -d \[tag=$prev\]" UNHEALTHY=all
check "unhealthy rollback is reported" \
    "$(grep -c "not healthy either - manual intervention needed" "$work/out.log")" -eq 1
run_case "failed rollback up -d still leaves .env on the previous tag" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "" FAIL_UP=all
check "failed rollback is reported" \
    "$(grep -c "rollback 'up -d --no-deps app' failed - manual intervention needed" "$work/out.log")" -eq 1

# Pre-deploy dumps past retention are pruned; nightly dumps are backup.sh's business.
seed_old_dumps() {
    touch -d '10 days ago' "$backups/pre-deploy-20000101T000000Z.dump" "$backups/2000-01-02.dump"
    touch -d '2 days ago' "$backups/pre-deploy-20000103T000000Z.dump"
}
PREPARE=seed_old_dumps
run_case "healthy deploy with old snapshots around" 0 "$new" "up -d \[tag=$new\]" ""
PREPARE=:
check "pre-deploy dumps past retention are pruned" ! -e "$backups/pre-deploy-20000101T000000Z.dump"
check "recent pre-deploy dumps are kept" -e "$backups/pre-deploy-20000103T000000Z.dump"
check "nightly dumps are left alone" -e "$backups/2000-01-02.dump"

# The rollback target read from .env must itself be a valid tag.
for bad_prev in latest "sha-1111111 sha-3333333" "sha-1111111;reboot" "sha-XYZ1234" ""; do
    prev="$bad_prev"
    run_case "invalid or missing current IMAGE_TAG '$bad_prev' aborts before any docker call" \
        1 "$bad_prev" "" "."
done
check "missing IMAGE_TAG is reported" \
    "$(grep -c "could not read current IMAGE_TAG" "$work/out.log")" -eq 1
prev=sha-1111111

# Digest-pinned tags (what CD sends): written to .env verbatim - the '@' and ':'
# must survive set_tag's sed in both directions (deploy and rollback).
digest_a="sha256:$(printf '0123456789abcdef%.0s' 1 2 3 4)"
digest_b="sha256:$(printf 'fedcba9876543210%.0s' 1 2 3 4)"
new="sha-2222222@$digest_a"
run_case "healthy deploy of a digest-pinned tag writes it to .env verbatim" 0 "$new" \
    "pull app migrate \[tag=$new\]" ""
prev="sha-1111111@$digest_b"
run_case "failed health check restores a digest-pinned previous tag verbatim" 1 "$prev" \
    "up -d --no-deps app \[tag=$prev\]" "" UNHEALTHY="$new"

# Malformed tags never reach docker and leave .env alone.
prev=sha-1111111
for bad in "sha-2222222@${digest_a%?}" "sha-2222222@sha512:${digest_a#sha256:}" \
    "sha-2222222@sha256:$(printf 'ABCDEF0123456789%.0s' 1 2 3 4)" "sha-2222222@$digest_a@$digest_b" \
    "latest"; do
    new="$bad"
    run_case "malformed tag '$bad' rejected before any docker call" 1 "$prev" "" "."
done

echo
echo "test_planner_deploy.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
