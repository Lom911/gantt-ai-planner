#!/usr/bin/env bash
# deploy/tests/test_bootstrap_secrets.sh
#
# Exercises bootstrap.sh's step_secrets (sourced, so main never runs) with
# SECRETS_DIR in a temp directory. 'openssl' is stubbed via PATH; chown and
# install's -o/-g are neutralised so this runs unprivileged. Nothing here
# touches a real server.
#
# Usage: bash deploy/tests/test_bootstrap_secrets.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

# shellcheck source=deploy/bootstrap.sh
source "$script_dir/../bootstrap.sh"
set -euo pipefail

SECRETS_DIR="$work/secrets"
stub_dir="$work/bin"
mkdir -p "$stub_dir"

# Unprivileged stand-ins: ownership is not what is under test here.
chown() { :; }
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

# openssl stub: OPENSSL_MODE=ok (random-looking line), empty (exit 0, no
# output) or fail (partial output, then exit 1).
cat > "$stub_dir/openssl" <<'STUB'
#!/usr/bin/env bash
case "${OPENSSL_MODE:-ok}" in
    ok) echo "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldA==" ;;
    empty) ;;
    fail) printf 'c2Vj' && exit 1 ;;
esac
exit 0
STUB
chmod +x "$stub_dir/openssl"

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
        ls -la "$SECRETS_DIR"
        sed 's/^/    out: /' "$work/out.log"
        fail=$((fail + 1))
    fi
}

# run_step <OPENSSL_MODE> - runs step_secrets in a subshell (it may `exit`)
run_step() {
    set +e
    (OPENSSL_MODE="$1" PATH="$stub_dir:$PATH" step_secrets) >"$work/out.log" 2>&1
    status=$?
    set -e
}
entries() { find "$SECRETS_DIR" -mindepth 1 | wc -l | tr -d ' '; }

for mode in fail empty; do
    rm -rf "$SECRETS_DIR"
    mkdir -p "$SECRETS_DIR"
    run_step "$mode"
    check "openssl '$mode': step_secrets exits non-zero" "$status" -ne 0
    check "openssl '$mode': no secret file and no temp file left" "$(entries)" -eq 0
done

rm -rf "$SECRETS_DIR"
mkdir -p "$SECRETS_DIR"
run_step ok
check "openssl ok: step_secrets succeeds" "$status" -eq 0
for name in db_app_password db_owner_password pg_superuser_password; do
    check "openssl ok: $name is non-empty" -s "$SECRETS_DIR/$name"
    case "$(uname -s)" in
        MINGW* | MSYS* | CYGWIN*) ;; # NTFS has no real POSIX modes; CI (Linux) checks this
        *) check "openssl ok: $name is mode 0444" "$(stat -c %a "$SECRETS_DIR/$name")" = 444 ;;
    esac
done
check "openssl ok: only the 3 passwords and 2 API-key placeholders, no temp files" "$(entries)" -eq 5

rm -f "$SECRETS_DIR/db_app_password" # 0444: replace rather than overwrite (not root here)
printf 'keep-me' > "$SECRETS_DIR/db_app_password"
run_step fail
check "existing secrets are left untouched (openssl is not even called)" "$status" -eq 0
check "existing secret content unchanged" "$(cat "$SECRETS_DIR/db_app_password")" = keep-me

echo
echo "test_bootstrap_secrets.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
