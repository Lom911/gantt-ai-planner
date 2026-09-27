#!/usr/bin/env bash
# deploy/tests/test_bootstrap_caddy.sh
#
# Exercises bootstrap.sh's check_caddy_stack (sourced, so main never runs)
# with CADDY_DIR in a temp directory. 'docker' is stubbed via PATH and prints
# a canned `docker compose config` (normalized) output, so no Docker is
# needed. Nothing here touches a real server.
#
# Usage: bash deploy/tests/test_bootstrap_caddy.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

# shellcheck source=deploy/bootstrap.sh
source "$script_dir/../bootstrap.sh"
set -euo pipefail

CADDY_DIR="$work/caddy"
stub_dir="$work/bin"
mkdir -p "$CADDY_DIR" "$stub_dir"
: > "$CADDY_DIR/compose.yml"

# docker stub: `docker compose -f <file> config` prints $CONFIG_FIXTURE, or
# fails like a broken compose file when CONFIG_FAILS=1.
cat > "$stub_dir/docker" <<'STUB'
#!/usr/bin/env bash
if [ "${CONFIG_FAILS:-0}" = 1 ]; then
    echo "yaml: line 3: mapping values are not allowed in this context" >&2
    exit 15
fi
cat "$CONFIG_FIXTURE"
STUB
chmod +x "$stub_dir/docker"

# What `docker compose config` prints for deploy/caddy/compose.yml (trimmed).
hardened_attached='name: caddy
services:
  caddy:
    cap_drop:
      - ALL
    image: caddy:2
    networks:
      edge: null
      planner-proxy: null
    read_only: true
networks:
  edge:
    name: edge
    external: true
  planner-proxy:
    name: planner-proxy
    external: true'
# An older shared stack: only on `edge`, not hardened, another site next to Caddy.
old_edge_only='name: caddy
services:
  caddy:
    image: caddy:2
    networks:
      edge: null
  whoami:
    image: traefik/whoami
    networks:
      planner-proxy-old:
        aliases:
          - planner-proxy
networks:
  edge:
    name: edge
    external: true
  planner-proxy-old:
    name: planner-proxy-old'
# Attached under another key whose real name is planner-proxy, with aliases.
aliased_key='name: caddy
services:
  proxy:
    image: caddy:2
    networks:
      edge: null
      internal-planner:
        aliases:
          - caddy
networks:
  edge:
    name: edge
    external: true
  internal-planner:
    name: planner-proxy
    external: true'

good_caddyfile='example.org {
	reverse_proxy other:80
}

gantt-ai-planner.duckdns.org {
	reverse_proxy planner-app:8000 {
		flush_interval -1
	}
}'
commented_caddyfile='example.org {
	reverse_proxy other:80
}
# gantt-ai-planner.duckdns.org {
#	reverse_proxy planner-app:8000
# }'

pass=0
fail=0
# run_check <desc> <expect_status> <config> <caddyfile> [grep for in output...]
run_check() {
    desc="$1" expect_status="$2"
    printf '%s\n' "$3" > "$work/config.yml"
    printf '%s\n' "$4" > "$CADDY_DIR/Caddyfile"
    shift 4
    set +e
    (CONFIG_FIXTURE="$work/config.yml" PATH="$stub_dir:$PATH" check_caddy_stack) >"$work/out.log" 2>&1
    status=$?
    set -e
    problem=""
    [ "$status" -eq "$expect_status" ] || problem="exit $status, expected $expect_status"
    for want in "$@"; do
        [ -z "$problem" ] && ! grep -qF -- "$want" "$work/out.log" && problem="output lacks '$want'"
    done
    if [ -n "$problem" ]; then
        echo "FAIL: $desc ($problem)"
        sed 's/^/    out: /' "$work/out.log"
        fail=$((fail + 1))
    else
        echo "PASS: $desc"
        pass=$((pass + 1))
    fi
}
no_output() { ! grep -qF -- "$1" "$work/out.log"; }
check() {
    if "${@:2}"; then echo "PASS: $1"; pass=$((pass + 1)); else echo "FAIL: $1"; fail=$((fail + 1)); fi
}

run_check "hardened stack on planner-proxy with the app site passes" 0 \
    "$hardened_attached" "$good_caddyfile" "attached to 'planner-proxy' and proxies to planner-app:8000"
check "hardened stack: no hardening note" no_output "NOTE:"

run_check "stack not on planner-proxy aborts with the exact fix" 1 \
    "$old_edge_only" "$good_caddyfile" \
    "ABORT: no service in $CADDY_DIR/compose.yml is attached to the 'planner-proxy' network" \
    "      - planner-proxy     # in addition" "docker network create --internal planner-proxy"
check "a network merely aliased 'planner-proxy' does not count" no_output "proxies to planner-app"

run_check "attached under another key named planner-proxy passes" 0 \
    "$aliased_key" "$good_caddyfile" "attached to 'planner-proxy'"
check "unhardened stack gets a NOTE, not an abort" grep -qF "NOTE: Caddy runs without the hardening" "$work/out.log"

run_check "Caddyfile without reverse_proxy planner-app:8000 aborts with the fix" 1 \
    "$hardened_attached" "$commented_caddyfile" \
    "ABORT: $CADDY_DIR/Caddyfile has no 'reverse_proxy planner-app:8000' line" "caddy validate"

run_check "both problems are reported in one run" 1 \
    "$old_edge_only" "$commented_caddyfile" "attached to the 'planner-proxy' network" "has no 'reverse_proxy"

run_check "http:// upstream form is accepted" 0 \
    "$hardened_attached" "site {
	reverse_proxy http://planner-app:8000
}" "proxies to planner-app:8000"

CONFIG_FAILS=1 run_check "broken compose.yml aborts and shows the compose error" 1 \
    "$hardened_attached" "$good_caddyfile" "ABORT: 'docker compose -f $CADDY_DIR/compose.yml config' fails" \
    "mapping values are not allowed"

echo
echo "test_bootstrap_caddy.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
