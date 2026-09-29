#!/usr/bin/env bash
# scripts/migration-compat.sh <new-image> <previous-image>
#
# N-1 migration compatibility check (CI job `e2e`; runs locally too):
# the NEW image migrates a fresh Postgres (`alembic upgrade head`, as the
# `migrate` service does in production), then the PREVIOUS release's image
# serves that schema and must pass a smoke test. That is exactly the state
# production is in after planner-deploy rolls back a release whose
# migrations already ran: the rollback restarts only `app` on the previous
# image and never un-migrates, so migrations must stay backward compatible -
# this check enforces it.
#
# Roles as in production (deploy/initdb/10-roles.sh): planner_owner runs the
# migrations, the app connects as planner_app (DML only). Everything lives in
# throwaway containers and a network named migcompat-<pid>, removed on exit;
# no host ports, no bind mounts.
#
# Smoke test (inside the previous app's container, stdlib only): /healthz,
# new session, GET /api/plan, one POST /api/plan/operations, GET
# /api/plan/export - plus a fake-LLM chat turn (writes chat_messages and
# chat_usage), issuing an MCP token twice (revoke + insert on mcp_tokens) and
# deleting the session (cascades over every per-session table, new ones
# included). Every call must answer 2xx; the chat stream must end in `done`.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <new-image> <previous-image>" >&2
    exit 2
fi
new_image="$1"
prev_image="$2"
PG_IMAGE=postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
roles_sql="$script_dir/../deploy/initdb/10-roles.sh"
export MSYS_NO_PATHCONV=1 # Git Bash: leave container paths alone

name="migcompat-$$"
net="$name"
db="$name-db"
app="$name-app"

cleanup() {
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] && docker inspect "$app" >/dev/null 2>&1; then
        echo "--- previous app's log (last 80 lines):" >&2
        docker logs --tail 80 "$app" >&2 2>&1 || true
    fi
    docker rm -f "$app" "$db" >/dev/null 2>&1 || true
    docker network rm "$net" >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT

rand() { head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
super_pw="$(rand)"
owner_pw="$(rand)"
app_pw="$(rand)"

echo "==> new image:      $new_image"
echo "==> previous image: $prev_image"

docker network create "$net" >/dev/null
POSTGRES_PASSWORD="$super_pw" docker run -d --name "$db" --network "$net" --network-alias db \
    -e POSTGRES_PASSWORD "$PG_IMAGE" >/dev/null
# TCP, not the socket: the entrypoint's temporary init server listens on the socket only.
for _ in $(seq 1 60); do
    docker exec "$db" pg_isready -h 127.0.0.1 -U postgres -q 2>/dev/null && break
    sleep 1
done
docker exec "$db" pg_isready -h 127.0.0.1 -U postgres -q

echo "==> production roles (deploy/initdb/10-roles.sh)"
printf '%s' "$owner_pw" | docker exec -i "$db" sh -c 'mkdir -p /run/secrets && cat > /run/secrets/db_owner_password'
printf '%s' "$app_pw" | docker exec -i "$db" sh -c 'cat > /run/secrets/db_app_password'
docker exec -i "$db" sh -c 'cat > /tmp/10-roles.sh' < "$roles_sql"
docker exec -e POSTGRES_USER=postgres "$db" sh /tmp/10-roles.sh

echo "==> NEW image: alembic upgrade head (as planner_owner)"
DB_PASSWORD="$owner_pw" docker run --rm --network "$net" \
    -e DB_HOST=db -e DB_PORT=5432 -e DB_NAME=planner -e DB_USER=planner_owner -e DB_PASSWORD \
    --read-only --tmpfs /tmp "$new_image" alembic upgrade head
echo "    schema at revision $(docker exec "$db" psql -U postgres -d planner -tAc 'SELECT version_num FROM alembic_version')"

echo "==> PREVIOUS image: app on the new schema (as planner_app, fake LLM)"
DB_PASSWORD="$app_pw" docker run -d --name "$app" --network "$net" \
    -e DB_HOST=db -e DB_PORT=5432 -e DB_NAME=planner -e DB_USER=planner_app -e DB_PASSWORD \
    -e LLM_PROVIDER=fake -e PUBLIC_ORIGIN=http://localhost:8000 \
    --read-only --tmpfs /tmp "$prev_image" >/dev/null
healthy=0
for _ in $(seq 1 60); do
    if [ "$(docker inspect -f '{{.State.Running}}' "$app")" != true ]; then
        echo "previous app exited on the new schema" >&2
        exit 1
    fi
    if docker exec "$app" python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" \
        >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 1
done
if [ "$healthy" -ne 1 ]; then
    echo "previous app did not become healthy within 60s" >&2
    exit 1
fi

echo "==> smoke test against the previous app"
docker exec -i "$app" python - <<'PY'
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
ORIGIN = "http://localhost:8000"  # = PUBLIC_ORIGIN, as a browser would send it
cookie = None


def call(method, path, body=None, timeout=30):
    global cookie
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Origin", ORIGIN)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, payload, set_cookie = r.status, r.read(), r.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        status, payload, set_cookie = e.code, e.read(), None
    if set_cookie:
        cookie = set_cookie.split(";", 1)[0]
    ok = 200 <= status < 300
    print(f"    {'ok  ' if ok else 'FAIL'} {method} {path} -> {status} ({len(payload)} bytes)")
    if not ok:
        print("         " + payload[:300].decode(errors="replace"))
        sys.exit(1)
    return payload


def fail(message):
    print(f"    FAIL {message}")
    sys.exit(1)


call("GET", "/healthz")
call("POST", "/api/session")
if not cookie:
    fail("POST /api/session set no cookie")
plan = json.loads(call("GET", "/api/plan"))
tasks = plan["plan"]["tasks"]
if not tasks:
    fail("the new session's plan has no tasks")
task = tasks[0]
op = {"op": "update_task", "id": task["id"], "duration": int(task.get("duration") or 1) + 1}
call("POST", "/api/plan/operations", {"ops": [op]})
call("GET", "/api/plan/export")

# chat_messages + chat_usage: one fake-LLM turn, which must end in `done`, not `error`.
stream = call("POST", "/api/chat", {"message": "Сдвинь задачу 1 на 1 день"}, timeout=120)
events = []
for block in stream.decode().replace("\r\n", "\n").split("\n\n"):
    data = [line[5:].strip() for line in block.split("\n") if line.startswith("data:")]
    if data:
        events.append(json.loads("\n".join(data)))
last = events[-1].get("type") if events else None
if last != "done":
    fail(f"chat stream ended with {last!r}: {events[-1] if events else 'no events'}")
print(f"    ok   chat stream: {len(events)} events, ends with done")

# mcp_tokens: the second issue revokes the first and inserts a new one.
call("POST", "/api/mcp-token")
call("POST", "/api/mcp-token")
call("GET", "/api/mcp-token")

# Cascades over every per-session table, including ones only the new schema has.
call("DELETE", "/api/session")
PY

echo "==> OK: the previous release runs on the schema the new release migrates to"
