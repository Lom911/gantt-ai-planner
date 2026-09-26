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
# /opt/gantt-planner and /opt/caddy stacks, and the backup cron job.
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
        printf 'IMAGE_TAG=latest\nLLM_PROVIDER=openrouter\nLLM_MODEL=anthropic/claude-sonnet-5\n' > "$APP_DIR/.env"
        chmod 0600 "$APP_DIR/.env"
        chown root:root "$APP_DIR/.env"
        echo "    created $APP_DIR/.env with IMAGE_TAG=latest, LLM_PROVIDER=openrouter, LLM_MODEL=anthropic/claude-sonnet-5"
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
}

step_secrets() {
    echo "==> Generating database secrets (only if missing)"
    for name in db_app_password db_owner_password pg_superuser_password; do
        secret_file="$SECRETS_DIR/$name"
        if [ ! -f "$secret_file" ]; then
            (umask 177 && openssl rand -base64 32 > "$secret_file")
            chmod 0444 "$secret_file"
            chown root:root "$secret_file"
            echo "    generated $secret_file"
        else
            echo "    $secret_file already exists, leaving it untouched"
        fi
    done

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
    echo "      3. point the gantt-ai-planner.duckdns.org A record at this host's IP"
    echo "      4. run the first deploy manually: set IMAGE_TAG=sha-<commit> in $APP_DIR/.env, then"
    echo "         cd $APP_DIR && docker compose -f compose.prod.yml up -d"
}

main "$@"
