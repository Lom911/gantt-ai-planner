#!/usr/bin/env bash
# deploy/tests/test_wrapper.sh
#
# Exercises planner-deploy-wrapper's SSH_ORIGINAL_COMMAND validation.
# 'sudo' is stubbed via PATH so nothing privileged ever runs, and the real
# planner-deploy is never invoked. Nothing here touches a real server.
#
# Usage: bash deploy/tests/test_wrapper.sh
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
wrapper="$script_dir/../planner-deploy-wrapper"
stub_dir="$(mktemp -d)"
log_file="$stub_dir/sudo.log"

cleanup() { rm -rf "$stub_dir"; }
trap cleanup EXIT

cat > "$stub_dir/sudo" <<'EOF'
#!/usr/bin/env bash
echo "sudo $*" >> "$(dirname "$0")/sudo.log"
exit 0
EOF
chmod +x "$stub_dir/sudo"

pass=0
fail=0

run_case() {
    desc="$1"
    input="$2"
    expect_status="$3"
    expect_log_substr="${4:-}"

    : > "$log_file"
    set +e
    SSH_ORIGINAL_COMMAND="$input" PATH="$stub_dir:$PATH" "$wrapper" >/dev/null 2>&1
    status=$?
    set -e

    if [ "$status" -ne "$expect_status" ]; then
        echo "FAIL: $desc (expected exit $expect_status, got $status)"
        fail=$((fail + 1))
        return
    fi

    if [ -n "$expect_log_substr" ]; then
        if ! grep -qF "$expect_log_substr" "$log_file"; then
            echo "FAIL: $desc (expected sudo call containing: $expect_log_substr)"
            fail=$((fail + 1))
            return
        fi
    elif [ -s "$log_file" ]; then
        echo "FAIL: $desc (expected sudo NOT to be called, but it was: $(cat "$log_file"))"
        fail=$((fail + 1))
        return
    fi

    echo "PASS: $desc"
    pass=$((pass + 1))
}

digest="sha256:$(printf '0123456789abcdef%.0s' 1 2 3 4)"
# Bare tags are mutable in GHCR: refused, for manual deploys and rollbacks too.
run_case "bare short sha tag rejected (no digest)" "sha-abc1234" 1
run_case "bare full-length sha tag rejected (no digest)" \
    "sha-0123456789abcdef0123456789abcdef01234567" 1
run_case "valid short sha tag pinned by digest" \
    "sha-abc1234@$digest" 0 "sudo /usr/local/bin/planner-deploy sha-abc1234@$digest"
run_case "valid full-length sha tag pinned by digest" \
    "sha-0123456789abcdef0123456789abcdef01234567@$digest" 0 \
    "sudo /usr/local/bin/planner-deploy sha-0123456789abcdef0123456789abcdef01234567@$digest"
run_case "digest one hex digit short rejected" "sha-abc1234@${digest%?}" 1
run_case "digest one hex digit long rejected" "sha-abc1234@${digest}0" 1
run_case "uppercase digest rejected" "sha-abc1234@sha256:$(printf 'ABCDEF0123456789%.0s' 1 2 3 4)" 1
run_case "sha512 digest rejected" "sha-abc1234@sha512:${digest#sha256:}" 1
run_case "digest without algorithm rejected" "sha-abc1234@${digest#sha256:}" 1
run_case "digest without a tag rejected" "$digest" 1
run_case "bare @digest without a tag rejected" "@$digest" 1
run_case "two digests rejected" "sha-abc1234@$digest@$digest" 1
run_case "digest followed by an extra token rejected" "sha-abc1234@$digest extra" 1
run_case "digest split off as a second token rejected" "sha-abc1234 @$digest" 1
run_case "empty command rejected" "" 1
run_case "wrong prefix rejected" "latest" 1
run_case "wrong prefix with a digest rejected" "latest@$digest" 1
run_case "hex too short rejected" "sha-abc12@$digest" 1
run_case "hex too long rejected" "sha-0123456789abcdef0123456789abcdef012345678@$digest" 1
run_case "uppercase hex rejected" "sha-ABC1234@$digest" 1
run_case "extra token rejected" "sha-abc1234@$digest extra" 1
run_case "shell metacharacters rejected" "sha-abc1234@$digest; rm -rf /" 1
run_case "glob character rejected" "sha-abc123*@$digest" 1
# Single-quoted on purpose: we want the literal string passed through,
# not expanded by this test script's own shell.
# shellcheck disable=SC2016
run_case "command substitution rejected" '$(rm -rf /)' 1

echo
echo "test_wrapper.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
