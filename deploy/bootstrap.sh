#!/usr/bin/env bash
# deploy/bootstrap.sh
#
# One-time (idempotent) setup for the gantt-planner production stack on
# the shared VPS. Run manually as root:
#
#   sudo bash deploy/bootstrap.sh
#
# Every step is safe to re-run and only echoes what it does. This script
# NEVER touches any other service on the host, never opens/closes ports,
# and never runs ufw - it only manages the 'deploy' user, the
# /opt/gantt-planner and /opt/caddy stacks, the Docker networks 'edge'
# (Caddy <-> other sites) and 'planner-proxy' (internal, Caddy <-> app
# only), and the backup cron job.
set -euo pipefail

APP_DIR=/opt/gantt-planner
CADDY_DIR=/opt/caddy
PG_DATA_DIR=/var/lib/gantt-planner/pg
BACKUP_DIR=/var/backups/gantt-planner
SECRETS_DIR="$APP_DIR/secrets"
DEPLOY_USER=deploy
PLANNER_DEPLOY_BIN=/usr/local/bin/planner-deploy
PLANNER_DEPLOY_WRAPPER_BIN=/usr/local/bin/planner-deploy-wrapper
BACKUP_BIN=/usr/local/bin/gantt-planner-backup.sh
SUDOERS_FILE=/etc/sudoers.d/gantt-planner-deploy
CRON_FILE=/etc/cron.d/gantt-planner-backup
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POSTGRES_UID=70
POSTGRES_GID=70

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "bootstrap.sh must be run as root (sudo bash deploy/bootstrap.sh)" >&2
        exit 1
    fi
}

step_user() {
    echo "==> Ensuring '$DEPLOY_USER' system user exists (not a member of the docker group)"
    if id "$DEPLOY_USER" >/dev/null 2>&1; then
        echo "    user '$DEPLOY_USER' already exists"
    else
        useradd --system --create-home --shell /bin/bash "$DEPLOY_USER"
        echo "    created user '$DEPLOY_USER'"
    fi

    install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
    if [ ! -f "/home/$DEPLOY_USER/.ssh/authorized_keys" ]; then
        install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null "/home/$DEPLOY_USER/.ssh/authorized_keys"
        echo "    created empty /home/$DEPLOY_USER/.ssh/authorized_keys"
        echo "    ACTION NEEDED: add the CI deploy key manually, e.g.:"
        echo "      restrict,command=\"$PLANNER_DEPLOY_WRAPPER_BIN\" ssh-ed25519 AAAA... gha-gantt-planner"
    else
        echo "    /home/$DEPLOY_USER/.ssh/authorized_keys already exists, leaving it untouched"
    fi
}

step_scripts() {
    echo "==> Installing planner-deploy and planner-deploy-wrapper"
    install -m 0755 -o root -g root "$SCRIPT_DIR/planner-deploy" "$PLANNER_DEPLOY_BIN"
    install -m 0755 -o root -g root "$SCRIPT_DIR/planner-deploy-wrapper" "$PLANNER_DEPLOY_WRAPPER_BIN"
    install -m 0755 -o root -g root "$SCRIPT_DIR/backup.sh" "$BACKUP_BIN"
    echo "    installed $PLANNER_DEPLOY_BIN, $PLANNER_DEPLOY_WRAPPER_BIN, $BACKUP_BIN"
}

step_sudoers() {
    echo "==> Installing sudoers rule for '$DEPLOY_USER'"
    tmp_sudoers="$(mktemp)"
    echo "$DEPLOY_USER ALL=(root) NOPASSWD: $PLANNER_DEPLOY_BIN *" > "$tmp_sudoers"
    if visudo -cf "$tmp_sudoers"; then
        install -m 0440 -o root -g root "$tmp_sudoers" "$SUDOERS_FILE"
        echo "    installed $SUDOERS_FILE"
    else
        echo "    generated sudoers snippet failed visudo validation, aborting" >&2
        rm -f "$tmp_sudoers"
        exit 1
    fi
    rm -f "$tmp_sudoers"
}

step_dirs() {
    echo "==> Creating application directories"
    install -d -m 0755 -o root -g root "$APP_DIR"
    install -d -m 0700 -o root -g root "$SECRETS_DIR"
    install -d -m 0700 -o root -g root "$BACKUP_DIR"
    install -d -m 0755 -o root -g root "$CADDY_DIR"
    install -d -m 0700 -o "$POSTGRES_UID" -g "$POSTGRES_GID" "$PG_DATA_DIR"
    echo "    ready: $APP_DIR, $SECRETS_DIR, $PG_DATA_DIR, $BACKUP_DIR, $CADDY_DIR"
}

step_compose_files() {
    echo "==> Installing compose files, initdb script and Caddy config"
    install -m 0644 -o root -g root "$SCRIPT_DIR/compose.prod.yml" "$APP_DIR/compose.prod.yml"
    install -d -m 0755 -o root -g root "$APP_DIR/initdb"
    install -m 0755 -o root -g root "$SCRIPT_DIR/initdb/10-roles.sh" "$APP_DIR/initdb/10-roles.sh"

    if [ ! -f "$APP_DIR/.env" ]; then
        # No IMAGE_TAG on purpose (no silent `latest`): compose.prod.yml refuses to run
        # without one, and the first release sets an explicit digest-pinned ref by hand
        # (docs/runbook.md section 1); planner-deploy maintains it from then on.
        (umask 177 && printf '%s\n' \
            '# IMAGE_TAG=sha-<commit>@sha256:<digest>  <- added by the first manual deploy (docs/runbook.md section 1)' \
            'LLM_PROVIDER=openrouter' \
            'LLM_MODEL=anthropic/claude-sonnet-5' > "$APP_DIR/.env")
        chmod 0600 "$APP_DIR/.env"
        chown root:root "$APP_DIR/.env"
        echo "    created $APP_DIR/.env with LLM_PROVIDER=openrouter, LLM_MODEL=anthropic/claude-sonnet-5 (no IMAGE_TAG yet)"
    else
        echo "    $APP_DIR/.env already exists, leaving it untouched"
    fi

    echo "    installed $APP_DIR/compose.prod.yml, $APP_DIR/initdb/10-roles.sh"

    # The Caddy stack may be shared with other sites on this host: never overwrite an existing
    # Caddyfile/compose.yml (a re-run would silently drop the other sites) — only create them.
    for file in compose.yml Caddyfile; do
        if [ -f "$CADDY_DIR/$file" ]; then
            echo "    $CADDY_DIR/$file already exists, leaving it untouched (merge deploy/caddy/$file by hand if needed)"
        else
            install -m 0644 -o root -g root "$SCRIPT_DIR/caddy/$file" "$CADDY_DIR/$file"
            echo "    installed $CADDY_DIR/$file"
        fi
    done
    check_caddy_stack
}

# The app is reachable only through Caddy on 'planner-proxy', as `planner-app:8000`. A
# shared Caddy stack that predates this setup (bootstrap never overwrites it) would lose
# the site as soon as the app is recreated, so refuse to continue - fail closed - until
# it is fixed, and print the exact fix.
check_caddy_stack() {
    echo "==> Checking the Caddy stack in $CADDY_DIR"
    local config problems=0
    if ! config="$(docker compose -f "$CADDY_DIR/compose.yml" config 2>&1)"; then
        echo "    ABORT: 'docker compose -f $CADDY_DIR/compose.yml config' fails:" >&2
        printf '%s\n' "$config" | sed 's/^/      /' >&2
        exit 1
    fi
    # Normalized config: service networks are always a map (`      <key>: ...` under
    # `    networks:`), and every top-level network has an explicit `name:` - so a network
    # attached under another key but named planner-proxy counts too.
    if ! printf '%s\n' "$config" | awk '
        /^[^ ]/ { section = $1; in_nets = 0; next }
        section == "services:" && /^  [^ ]/ { in_nets = 0 }
        section == "services:" && /^    [^ ]/ { in_nets = ($1 == "networks:") }
        section == "services:" && in_nets && /^      [^ -]/ { k = $1; sub(/:$/, "", k); used[k] = 1 }
        section == "networks:" && /^  [^ ]/ { cur = $1; sub(/:$/, "", cur) }
        section == "networks:" && /^    name: / { name[cur] = $2 }
        END { for (k in used) if (name[k] == "planner-proxy") found = 1; exit !found }'; then
        problems=1
        cat >&2 <<EOF
    ABORT: no service in $CADDY_DIR/compose.yml is attached to the 'planner-proxy' network,
    so Caddy cannot reach the app (gantt-ai-planner.duckdns.org would answer 502).
    Fix: add to $CADDY_DIR/compose.yml (keep everything else as it is):

        networks:
          planner-proxy:
            external: true
        services:
          caddy:
            networks:
              - planner-proxy     # in addition to the networks it already has

    then recreate Caddy (a few seconds of downtime for every site on this host) and re-run:
        docker network inspect planner-proxy >/dev/null 2>&1 || docker network create --internal planner-proxy
        cd $CADDY_DIR && docker compose config -q && docker compose up -d
EOF
    fi
    if ! grep -Eq '^[[:space:]]*reverse_proxy[[:space:]]+(http://)?planner-app:8000([[:space:]{]|$)' "$CADDY_DIR/Caddyfile"; then
        problems=1
        cat >&2 <<EOF
    ABORT: $CADDY_DIR/Caddyfile has no 'reverse_proxy planner-app:8000' line, so no site
    sends traffic to the app. Fix: add the gantt-ai-planner.duckdns.org site block from
    deploy/caddy/Caddyfile (keep the other sites), then restart Caddy (admin API is off;
    a few seconds of downtime for every site on this host) and re-run:
        cd $CADDY_DIR && docker compose run --rm --no-deps caddy caddy validate \\
            --config /etc/caddy/Caddyfile --adapter caddyfile && docker compose restart caddy
EOF
    fi
    [ "$problems" -eq 0 ] || exit 1
    echo "    Caddy is attached to 'planner-proxy' and proxies to planner-app:8000"
    if ! printf '%s\n' "$config" | grep -q '^    read_only: true$'; then
        echo "    NOTE: Caddy runs without the hardening from deploy/caddy/compose.yml (read_only,"
        echo "    cap_drop ALL + NET_BIND_SERVICE, no-new-privileges, pids_limit); merge it by hand"
        echo "    (docs/runbook.md section 1, 'Существующий стек Caddy')."
    fi
}

# generate_secret <name> <openssl rand args...>: creates $SECRETS_DIR/<name> (0444,
# root) from `openssl rand <args>` unless it already exists. Returns 0 if it generated
# the file, 1 if it was already there; exits the script if generation fails.
generate_secret() {
    local name="$1" secret_file tmp_secret
    shift
    secret_file="$SECRETS_DIR/$name"
    if [ -f "$secret_file" ]; then
        echo "    $secret_file already exists, leaving it untouched"
        return 1
    fi
    # A compose `up` that ran while the file was missing makes Docker create an (empty)
    # directory in its place; only an empty one is removed.
    if [ -d "$secret_file" ]; then
        if rmdir "$secret_file" 2>/dev/null; then
            echo "    removed the empty directory Docker created in place of the missing $secret_file"
        else
            echo "    $secret_file is a non-empty directory, not a secret file - fix by hand, aborting" >&2
            exit 1
        fi
    fi
    # Generate into a temp file in the same directory and rename it into place only
    # once it is complete and non-empty: a failed/killed openssl must never leave an
    # empty secret file behind, which the next run would treat as existing (Postgres
    # would initialise the role with an empty password, the ops endpoint would accept
    # an empty token).
    # Every step is checked explicitly: callers use `if`/`||`, which suspends set -e here.
    if tmp_secret="$(mktemp "$SECRETS_DIR/.$name.XXXXXX")" &&
        (umask 177 && openssl rand "$@" > "$tmp_secret") && [ -s "$tmp_secret" ] &&
        chmod 0444 "$tmp_secret" && chown root:root "$tmp_secret" &&
        mv -f "$tmp_secret" "$secret_file"; then
        echo "    generated $secret_file"
        return 0
    fi
    [ -z "${tmp_secret:-}" ] || rm -f "$tmp_secret"
    echo "    FAILED to generate $secret_file (openssl rand), aborting" >&2
    exit 1
}

step_secrets() {
    echo "==> Generating database secrets (only if missing)"
    for name in db_app_password db_owner_password pg_superuser_password; do
        generate_secret "$name" -base64 32 || true
    done

    echo "==> Generating the ops endpoint token (only if missing)"
    if generate_secret ops_token -hex 32; then
        echo "    ACTION NEEDED: copy it into the GitHub Actions secret OPS_TOKEN (Settings -> Secrets"
        echo "    and variables -> Actions) so the Uptime workflow can read /api/ops/status:"
        echo "      cat $SECRETS_DIR/ops_token   # paste the value; do not put it in a command line"
    fi

    echo "==> Ensuring anthropic_api_key secret placeholder exists"
    anthropic_file="$SECRETS_DIR/anthropic_api_key"
    if [ ! -f "$anthropic_file" ]; then
        install -m 0444 -o root -g root /dev/null "$anthropic_file"
        echo "    created empty $anthropic_file - the owner must fill in the real key before starting the app"
    else
        echo "    $anthropic_file already exists, leaving it untouched"
    fi

    echo "==> Ensuring openrouter_api_key secret placeholder exists"
    openrouter_file="$SECRETS_DIR/openrouter_api_key"
    if [ ! -f "$openrouter_file" ]; then
        install -m 0444 -o root -g root /dev/null "$openrouter_file"
        echo "    created empty $openrouter_file - the owner must fill in the real key before starting the app"
    else
        echo "    $openrouter_file already exists, leaving it untouched"
    fi
}

step_network() {
    echo "==> Ensuring docker network 'edge' exists"
    if docker network inspect edge >/dev/null 2>&1; then
        echo "    network 'edge' already exists"
    else
        docker network create edge
        echo "    created network 'edge'"
    fi

    # Caddy <-> app only. --internal: no gateway, so nothing reaches the internet
    # through it (the app has its own project-local `egress` network for that).
    echo "==> Ensuring internal docker network 'planner-proxy' exists"
    if docker network inspect planner-proxy >/dev/null 2>&1; then
        if [ "$(docker network inspect -f '{{.Internal}}' planner-proxy)" = "true" ]; then
            echo "    network 'planner-proxy' already exists (internal)"
        else
            echo "    network 'planner-proxy' exists but is NOT internal - recreate it with" >&2
            echo "    'docker network create --internal planner-proxy' (detach caddy and app first)" >&2
            exit 1
        fi
    else
        docker network create --internal planner-proxy
        echo "    created internal network 'planner-proxy'"
    fi
}

step_caddy() {
    echo "==> Starting/refreshing the shared Caddy reverse-proxy stack"
    (cd "$CADDY_DIR" && docker compose up -d)
}

step_backup_cron() {
    echo "==> Installing nightly backup cron job"
    printf '15 3 * * * root %s >> /var/log/gantt-planner-backup.log 2>&1\n' "$BACKUP_BIN" > "$CRON_FILE"
    chmod 0644 "$CRON_FILE"
    chown root:root "$CRON_FILE"
    echo "    installed $CRON_FILE"
}

main() {
    require_root
    step_user
    step_scripts
    step_sudoers
    step_dirs
    step_compose_files
    step_secrets
    step_network
    step_caddy
    step_backup_cron
    echo "==> Bootstrap complete."
    echo "    Remaining manual steps:"
    echo "      1. fill in $SECRETS_DIR/openrouter_api_key (default: LLM_PROVIDER=openrouter in"
    echo "         $APP_DIR/.env) or $SECRETS_DIR/anthropic_api_key if using a real Anthropic key instead"
    echo "      2. add the CI deploy key to /home/$DEPLOY_USER/.ssh/authorized_keys (see step above)"
    echo "         and copy $SECRETS_DIR/ops_token into the GitHub Actions secret OPS_TOKEN"
    echo "      3. point the gantt-ai-planner.duckdns.org A record at this host's IP"
    echo "      4. run the first deploy manually with a digest-pinned ref (docs/runbook.md section 1);"
    echo "         the digest: docker buildx imagetools inspect ghcr.io/alomaev-hue/gantt-ai-planner:sha-<commit> --format '{{json .Manifest}}'"
    echo "         echo 'IMAGE_TAG=sha-<commit>@sha256:<digest>' >> $APP_DIR/.env"
    echo "         cd $APP_DIR && docker compose -f compose.prod.yml pull && docker compose -f compose.prod.yml up -d"
}

# Run only when executed (`bash deploy/bootstrap.sh`), not when sourced:
# deploy/tests/test_bootstrap_secrets.sh sources this file to test single steps.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
