#!/usr/bin/env bash
# scripts/ci-passed.sh <owner/repo> <commit-sha>
#
# Gate for a manual Deploy run (workflow_dispatch, .github/workflows/deploy.yml;
# runs locally too, read-only, with an authenticated `gh`): passes only if the
# `CI` workflow (.github/workflows/ci.yml) has a completed run with conclusion
# `success` for exactly this commit on a push to `main` of this repository -
# the same kind of run the workflow_run path deploys after. A PR run (event
# `pull_request`) or a fork's run never counts. If there are several such runs
# (the same commit pushed again), the latest one decides: an older success does
# not outweigh a newer failure. A run still queued or in progress fails too -
# wait for it and dispatch again (no polling here).
#
# Exit codes: 0 - CI passed; 1 - it did not (no run, failed, cancelled, still
# running) or the API call failed; 2 - bad arguments. Needs a token that can
# read Actions: GH_TOKEN (in Deploy - GITHUB_TOKEN with `actions: read`) or
# `gh auth login`. Tests: deploy/tests/test_ci_passed.sh.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <owner/repo> <commit-sha>" >&2
    exit 2
fi
repo="$1"
sha="$2"
# Both go into the URL and the jq filter below: accept only their exact shape.
if ! [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "not an owner/repo: '$repo'" >&2
    exit 2
fi
if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "not a full lowercase 40-hex commit SHA: '$sha'" >&2
    exit 2
fi

# The query parameters filter on the server; the filter checks the same
# conditions on every run again, so a parameter the API ignored cannot let
# another commit's, a PR's or a fork's run through. Repository names are
# case-insensitive on GitHub.
endpoint="repos/$repo/actions/workflows/ci.yml/runs?head_sha=$sha&event=push&branch=main&per_page=100"
filter='[.workflow_runs[]
    | select(.head_sha == "'"$sha"'" and .event == "push" and .head_branch == "main"
             and ((.head_repository.full_name // "") | ascii_downcase) == "'"${repo,,}"'")]
  | max_by(.id)
  | if . == null then "none" else "\(.status) \(.conclusion // "-") \(.html_url)" end'
if ! latest="$(gh api "$endpoint" --jq "$filter")"; then
    echo "::error::could not list CI runs for $sha in $repo (gh error above)"
    exit 1
fi

read -r status conclusion url <<< "$latest"
case "$status" in
    none)
        echo "::error::CI has not passed for $sha: no run (no CI run on a push to main of $repo for this commit)"
        exit 1
        ;;
    completed) ;;
    queued | in_progress | waiting | requested | pending)
        echo "::error::CI is still running for $sha ($status): wait for it to finish, then run Deploy again - $url"
        exit 1
        ;;
    *)
        echo "::error::unexpected CI run status for $sha: '$latest'"
        exit 1
        ;;
esac
if [ "$conclusion" != success ]; then
    echo "::error::CI has not passed for $sha: $conclusion - $url"
    exit 1
fi
echo "CI passed for $sha: $url"
