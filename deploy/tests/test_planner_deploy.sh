#!/usr/bin/env bash
# deploy/tests/test_planner_deploy.sh
#
# Exercises planner-deploy's pull / up / health-check / rollback flow against
# a throw-away copy of the script whose APP_DIR points at a temp directory.
# 'docker' and 'sleep' are stubbed via PATH: nothing real is pulled or
# started, and nothing here touches a real server.
#
# Usage: bash deploy/tests/test_planner_deploy.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

app_dir="$work/app"
stub_dir="$work/bin"
mkdir -p "$app_dir" "$stub_dir"
sed -e "s|^APP_DIR=.*|APP_DIR=\"$app_dir\"|" \
    -e "s|^HEALTH_TIMEOUT=.*|HEALTH_TIMEOUT=3|" \
    "$script_dir/../planner-deploy" > "$work/planner-deploy"
chmod +x "$work/planner-deploy"
: > "$app_dir/compose.prod.yml"

# docker stub: logs each call with the tag Compose would use (IMAGE_TAG from
# the environment wins over .env, as in Compose), and fails on demand:
#   FAIL_PULL=1          -> every `compose pull` fails
#   FAIL_UP=<tag>|all    -> `compose up` fails while that tag is in effect / always
#   HEALTHY=0            -> the in-container /healthz probe always fails
cat > "$stub_dir/docker" <<'STUB'
#!/usr/bin/env bash
env_tag="$(grep -E '^IMAGE_TAG=' .env | tail -n1 | cut -d= -f2-)"
tag="${IMAGE_TAG:-$env_tag}"
echo "$* [tag=$tag]" >> "$STUB_LOG"
case " $* " in
    *" pull "*) [ "${FAIL_PULL:-0}" = 1 ] && exit 1 ;;
    *" up "*) case "${FAIL_UP:-}" in all | "$tag") exit 1 ;; esac ;;
    *" exec "*) [ "${HEALTHY:-1}" = 1 ] || exit 1 ;;
esac
exit 0
STUB
printf '#!/usr/bin/env bash\nexit 0\n' > "$stub_dir/sleep"
chmod +x "$stub_dir/docker" "$stub_dir/sleep"

pass=0
fail=0
prev=sha-1111111
new=sha-2222222

# run_case <desc> <expect_status> <expect_env_tag> <expect_log_regex|""> <reject_log_regex|""> [VAR=value...]
run_case() {
    desc="$1" expect_status="$2" expect_tag="$3" want="$4" reject="$5"
    shift 5
    printf 'COMPOSE_PROJECT_NAME=gantt-planner\nIMAGE_TAG=%s\n' "$prev" > "$app_dir/.env"
    export STUB_LOG="$work/docker.log"
    : > "$STUB_LOG"
    set +e
    env "$@" PATH="$stub_dir:$PATH" "$work/planner-deploy" "$new" >"$work/out.log" 2>&1
    status=$?
    set -e
    got_tag="$(grep -E '^IMAGE_TAG=' "$app_dir/.env" | tail -n1 | cut -d= -f2-)"
    problem=""
    [ "$status" -eq "$expect_status" ] || problem="exit $status, expected $expect_status"
    [ -z "$problem" ] && [ "$got_tag" != "$expect_tag" ] && problem=".env has '$got_tag', expected '$expect_tag'"
    [ -z "$problem" ] && ! grep -q "^COMPOSE_PROJECT_NAME=gantt-planner$" "$app_dir/.env" && problem=".env lost other lines"
    [ -z "$problem" ] && [ -n "$want" ] && ! grep -qE "$want" "$STUB_LOG" && problem="no docker call matching /$want/"
    [ -z "$problem" ] && [ -n "$reject" ] && grep -qE "$reject" "$STUB_LOG" && problem="unexpected docker call matching /$reject/"
    if [ -n "$problem" ]; then
        echo "FAIL: $desc ($problem)"
        sed 's/^/    docker /' "$STUB_LOG"
        sed 's/^/    out: /' "$work/out.log"
        fail=$((fail + 1))
    else
        echo "PASS: $desc"
        pass=$((pass + 1))
    fi
}

run_case "healthy deploy keeps the new tag" 0 "$new" \
    "pull app migrate \[tag=$new\]" "up -d \[tag=$prev\]"
run_case "failed pull leaves .env and the running stack alone" 1 "$prev" \
    "pull app migrate \[tag=$new\]" "up -d" FAIL_PULL=1
run_case "failed up -d restores the previous tag and restarts it" 1 "$prev" \
    "up -d \[tag=$prev\]" "" FAIL_UP="$new"
run_case "failed health check restores the previous tag and restarts it" 1 "$prev" \
    "up -d \[tag=$prev\]" "" HEALTHY=0
run_case "failed rollback up -d still leaves .env on the previous tag" 1 "$prev" \
    "up -d \[tag=$prev\]" "" FAIL_UP=all

# Digest-pinned tags (what CD sends): written to .env verbatim - the '@' and ':'
# must survive set_tag's sed in both directions (deploy and rollback).
digest_a="sha256:$(printf '0123456789abcdef%.0s' 1 2 3 4)"
digest_b="sha256:$(printf 'fedcba9876543210%.0s' 1 2 3 4)"
new="sha-2222222@$digest_a"
run_case "healthy deploy of a digest-pinned tag writes it to .env verbatim" 0 "$new" \
    "pull app migrate \[tag=$new\]" ""
prev="sha-1111111@$digest_b"
run_case "failed health check restores a digest-pinned previous tag verbatim" 1 "$prev" \
    "up -d \[tag=$prev\]" "" HEALTHY=0

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
