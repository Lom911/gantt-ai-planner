#!/usr/bin/env bash
# deploy/tests/test_ci_passed.sh
#
# Exercises scripts/ci-passed.sh (the manual-Deploy gate) against canned API
# answers. 'gh' is stubbed via PATH: it logs its arguments and answers
# `gh api <endpoint> --jq <filter>` by running the script's own filter over a
# fixture with the real jq (gh has jq built in), so the filter itself is under
# test. Nothing here touches GitHub.
#
# Usage: bash deploy/tests/test_ci_passed.sh   (needs jq)
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
gate="$script_dir/../../scripts/ci-passed.sh"
if ! command -v jq >/dev/null 2>&1; then
    echo "test_ci_passed.sh: needs jq (the gh stub evaluates the gate's --jq filter with it)" >&2
    exit 1
fi
work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

stub_dir="$work/bin"
mkdir -p "$stub_dir"
export STUB_LOG="$work/gh.log"
export FIXTURE="$work/runs.json"

cat > "$stub_dir/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
if [ "${FAIL_GH:-0}" = 1 ]; then
    echo "gh: Resource not accessible by integration (HTTP 403)" >&2
    exit 1
fi
[ "${1:-}" = api ] || exit 64
filter=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = --jq ]; then filter="${2:-}"; fi
    shift
done
[ -n "$filter" ] || exit 64
exec jq -r "$filter" "$FIXTURE"
STUB
chmod +x "$stub_dir/gh"

repo="Owner/planner"
sha="0123456789abcdef0123456789abcdef01234567"
other_sha="89abcdef0123456789abcdef0123456789abcdef"

# run_json <id> <status> <conclusion|null> [head_sha] [event] [head_branch] [head_repo]
run_json() {
    local conclusion="$3"
    [ "$conclusion" = null ] || conclusion="\"$conclusion\""
    printf '{"id":%s,"status":"%s","conclusion":%s,"head_sha":"%s","event":"%s","head_branch":"%s","head_repository":{"full_name":"%s"},"html_url":"https://github.com/%s/actions/runs/%s"}' \
        "$1" "$2" "$conclusion" "${4:-$sha}" "${5:-push}" "${6:-main}" "${7:-$repo}" "$repo" "$1"
}
# fixture <run-json>... - the API answer the gh stub serves
fixture() {
    local IFS=,
    printf '{"total_count":%s,"workflow_runs":[%s]}' "$#" "$*" > "$FIXTURE"
}

pass=0
fail=0
# run_case <desc> <expected exit> <expected output substring> [VAR=value...] [-- gate args]
run_case() {
    local desc="$1" expect_status="$2" expect_out="$3"
    shift 3
    local envs=() args=("$repo" "$sha")
    while [ "$#" -gt 0 ] && [ "$1" != -- ]; do envs+=("$1") && shift; done
    [ "$#" -eq 0 ] || { shift && args=("$@"); }
    : > "$STUB_LOG"
    set +e
    env "${envs[@]}" PATH="$stub_dir:$PATH" bash "$gate" "${args[@]}" > "$work/out.log" 2>&1
    status=$?
    set -e
    if [ "$status" -ne "$expect_status" ]; then
        echo "FAIL: $desc (expected exit $expect_status, got $status)"
        sed 's/^/    out: /' "$work/out.log"
        fail=$((fail + 1))
        return
    fi
    if ! grep -qF -- "$expect_out" "$work/out.log"; then
        echo "FAIL: $desc (expected output containing: $expect_out)"
        sed 's/^/    out: /' "$work/out.log"
        fail=$((fail + 1))
        return
    fi
    echo "PASS: $desc"
    pass=$((pass + 1))
}
calls() { grep -c '^gh ' "$STUB_LOG" || true; }
check() {
    local desc="$1"
    shift
    if test "$@"; then
        echo "PASS: $desc"
        pass=$((pass + 1))
    else
        echo "FAIL: $desc (test $*)"
        sed 's/^/    calls: /' "$STUB_LOG"
        fail=$((fail + 1))
    fi
}

fixture "$(run_json 11 completed success)"
run_case "successful push run on main passes" 0 "CI passed for $sha: https://github.com/$repo/actions/runs/11"
check "asks the CI workflow's runs, filtered to this commit on a push to main" \
    "$(grep -cF "gh api repos/$repo/actions/workflows/ci.yml/runs?head_sha=$sha&event=push&branch=main&per_page=100 --jq " "$STUB_LOG")" -eq 1

fixture
run_case "no run at all fails" 1 "::error::CI has not passed for $sha: no run"

fixture "$(run_json 12 completed failure)"
run_case "failed run fails" 1 "::error::CI has not passed for $sha: failure - https://github.com/$repo/actions/runs/12"
fixture "$(run_json 13 completed cancelled)"
run_case "cancelled run fails" 1 "::error::CI has not passed for $sha: cancelled"
fixture "$(run_json 14 completed skipped)"
run_case "skipped run fails" 1 "::error::CI has not passed for $sha: skipped"

fixture "$(run_json 15 in_progress null)"
run_case "run in progress fails with a wait message" 1 \
    "::error::CI is still running for $sha (in_progress): wait for it to finish"
fixture "$(run_json 16 queued null)"
run_case "queued run fails with a wait message" 1 "::error::CI is still running for $sha (queued)"
fixture "$(run_json 17 weird null)"
run_case "unknown run status fails" 1 "::error::unexpected CI run status for $sha"

# Runs the API should have filtered out never count, should a filter be ignored.
fixture "$(run_json 21 completed success "$sha" pull_request)"
run_case "successful PR run does not count" 1 "no run"
fixture "$(run_json 22 completed success "$sha" push main fork/planner)"
run_case "successful fork run does not count" 1 "no run"
fixture "$(run_json 23 completed success "$sha" push feature)"
run_case "successful push run on another branch does not count" 1 "no run"
fixture "$(run_json 24 completed success "$other_sha")"
run_case "successful run of another commit does not count" 1 "no run"
fixture "$(run_json 25 completed success "$sha" push main owner/PLANNER)"
run_case "repository name compared case-insensitively" 0 "CI passed for $sha"
fixture "$(run_json 26 completed success "$sha" pull_request)" "$(run_json 27 completed failure)"
run_case "a PR's success does not hide the push run's failure" 1 "CI has not passed for $sha: failure"

# Several push runs of the same commit: the latest (highest id) decides, in any order.
fixture "$(run_json 32 completed failure)" "$(run_json 31 completed success)"
run_case "newer failure beats older success" 1 "CI has not passed for $sha: failure"
fixture "$(run_json 41 completed failure)" "$(run_json 42 completed success)"
run_case "newer success beats older failure" 0 "CI passed for $sha: https://github.com/$repo/actions/runs/42"
fixture "$(run_json 51 completed success)" "$(run_json 52 in_progress null)"
run_case "newer run in progress means wait" 1 "CI is still running for $sha (in_progress)"

run_case "API error fails the gate" 1 "::error::could not list CI runs for $sha in $repo" FAIL_GH=1

run_case "short SHA rejected" 2 "not a full lowercase 40-hex commit SHA" -- "$repo" "${sha:0:7}"
check "gh not called for a short SHA" "$(calls)" -eq 0
run_case "uppercase SHA rejected" 2 "not a full lowercase" -- "$repo" "$(printf '%s' "$sha" | tr 'a-f' 'A-F')"
run_case "branch name instead of a SHA rejected" 2 "not a full lowercase" -- "$repo" main
run_case "repository without owner rejected" 2 "not an owner/repo" -- planner "$sha"
run_case "repository with a quote rejected" 2 "not an owner/repo" -- 'Owner/pl"anner' "$sha"
run_case "repository with an extra path segment rejected" 2 "not an owner/repo" -- "$repo/x" "$sha"
check "gh not called for a bad repository" "$(calls)" -eq 0
run_case "missing argument rejected" 2 "usage:" -- "$repo"

echo
echo "test_ci_passed.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
