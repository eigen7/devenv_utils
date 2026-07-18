#!/usr/bin/env bash
# Entrypoint for the devenv-gitea service container (see GITEA.md).
#
# Provisions the bind-mounted state dir on first boot (app.ini, sqlite
# database, admin + claude users with credential files), enforces the
# settings that must hold for service-container operation on every boot, and
# runs `gitea web` plus the header-stamping nginx front. If either process
# dies the container exits, so the Docker restart policy revives both.
#
# The state dir is always mounted at /workspace/mount/gitea: app.ini records
# absolute paths under it (WORK_PATH, database, repos, logs), so a fixed
# mount point makes any state dir work without path rewriting.
#
# Environment (all optional; used only when provisioning something absent):
#   HOST_UID / HOST_GID              UID/GID to run as, so state files land
#                                    owned by the host user (default 1000).
#   DEVENV_GITEA_ADMIN_USER / _EMAIL admin identity for first-time creation.
#   DEVENV_GITEA_ROOT_URL            public base URL stamped into app.ini
#                                    (the host's chosen web port).
set -euo pipefail

HOST_UID=${HOST_UID:-1000}
HOST_GID=${HOST_GID:-1000}
ADMIN_USER=${DEVENV_GITEA_ADMIN_USER:-dev}
ADMIN_EMAIL=${DEVENV_GITEA_ADMIN_EMAIL:-dev@localhost}
ROOT_URL=${DEVENV_GITEA_ROOT_URL:-http://localhost:3000/}

ROOT=/workspace/mount/gitea
APP_INI=$ROOT/app.ini
CREDS_DIR=$ROOT/credentials
NGINX_CONF=$ROOT/nginx/nginx.conf

# Fixed in-container ports; the host maps its chosen ports onto them.
WEB_PORT=3000      # nginx, stamping the reverse-proxy auth header
BACKEND_PORT=3001  # gitea itself (basic auth)

CLAUDE_USER=claude
# Matches the Claude commit identity, so Gitea links commits to the user.
CLAUDE_EMAIL=noreply@anthropic.com

# ---- Run as the host user's UID/GID, so state files stay host-owned -----
getent group "$HOST_GID" >/dev/null || groupadd -g "$HOST_GID" gitea
getent passwd "$HOST_UID" >/dev/null || useradd -m -u "$HOST_UID" -g "$HOST_GID" gitea
RUN_USER=$(getent passwd "$HOST_UID" | cut -d: -f1)

mkdir -p "$ROOT"
chown "$HOST_UID:$HOST_GID" "$ROOT"

as_user() { gosu "$RUN_USER" "$@"; }
run_gitea() { as_user gitea "$@" --config "$APP_INI" --work-path "$ROOT"; }

# ---- app.ini -------------------------------------------------------------
# Written only when absent: Gitea appends generated secrets to it on first
# start, so an existing file is never regenerated. The settings that must
# hold regardless of the file's origin are enforced by sed below:
# HTTP_ADDR must be 0.0.0.0 (dev containers reach the backend across the
# devenv Docker network) and ROOT_URL must match the host's chosen web port.
#
# ENABLE_PUSH_CREATE_USER lets an initial `git push` create a server-side
# repo, so registration needs no API calls; repos are created public
# (DEFAULT_PUSH_CREATE_PRIVATE = false) so API reads need no auth.
# REVERSE_PROXY_TRUSTED_PROXIES = 127.0.0.0/8 means only nginx -- loopback
# within this container's network namespace -- can stamp the auth header;
# requests over the Docker network carry a container source IP and fall back
# to basic auth. Year-long sessions keep non-header auth state (e.g. API
# tokens created later) from expiring under a single user.
if [ ! -f "$APP_INI" ]; then
  as_user tee "$APP_INI" >/dev/null << EOF
APP_NAME = Dev Forge
RUN_MODE = prod
WORK_PATH = $ROOT

[server]
HTTP_ADDR = 0.0.0.0
HTTP_PORT = $BACKEND_PORT
ROOT_URL = $ROOT_URL
DISABLE_SSH = true
OFFLINE_MODE = true

[database]
DB_TYPE = sqlite3
PATH = $ROOT/data/gitea.db

[repository]
ROOT = $ROOT/repos
ENABLE_PUSH_CREATE_USER = true
DEFAULT_PUSH_CREATE_PRIVATE = false

[security]
INSTALL_LOCK = true
REVERSE_PROXY_TRUSTED_PROXIES = 127.0.0.0/8
LOGIN_REMEMBER_DAYS = 365

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = false
ENABLE_REVERSE_PROXY_AUTHENTICATION = true

[log]
MODE = file
ROOT_PATH = $ROOT/log

[session]
PROVIDER = file
SESSION_LIFE_TIME = 31536000

[actions]
ENABLED = false

[cron.update_checker]
ENABLED = false
EOF
fi

sed -i -E \
  -e "s|^HTTP_ADDR *=.*|HTTP_ADDR = 0.0.0.0|" \
  -e "s|^HTTP_PORT *=.*|HTTP_PORT = $BACKEND_PORT|" \
  -e "s|^ROOT_URL *=.*|ROOT_URL = $ROOT_URL|" \
  "$APP_INI"

# ---- Database + users ----------------------------------------------------
# `gitea migrate` initializes the database on first boot and applies schema
# migrations after a Gitea version bump; it is a no-op otherwise.
run_gitea migrate

# Credential files live in credentials/ -- the one subdirectory that gets
# bind-mounted (read-only) into dev containers. A state dir provisioned
# before the subdirectory existed has them at the state root; relocate.
as_user mkdir -p "$CREDS_DIR"
for f in admin_credentials.json claude_credentials.json; do
  if [ -f "$ROOT/$f" ] && [ ! -f "$CREDS_DIR/$f" ]; then
    mv "$ROOT/$f" "$CREDS_DIR/$f"
  fi
done

random_password() { head -c 18 /dev/urandom | base64 | tr '+/' '-_'; }

# create_user <creds-file> <username> <email> [--admin]
create_user() {
  local creds_file=$1 username=$2 email=$3 admin_flag=${4:-}
  if [ -f "$creds_file" ]; then
    return
  fi
  local password
  password=$(random_password)
  run_gitea admin user create $admin_flag \
    --username "$username" --password "$password" --email "$email" \
    --must-change-password=false
  as_user tee "$creds_file" >/dev/null << EOF
{
  "username": "$username",
  "password": "$password"
}
EOF
  chmod 600 "$creds_file"
}

create_user "$CREDS_DIR/admin_credentials.json" "$ADMIN_USER" "$ADMIN_EMAIL" --admin
create_user "$CREDS_DIR/claude_credentials.json" "$CLAUDE_USER" "$CLAUDE_EMAIL"

# ---- nginx ---------------------------------------------------------------
# Regenerated every boot (it is derived state): fronts Gitea on the web port
# and stamps every request with the admin user's reverse-proxy auth header,
# so anyone who can reach the web port is permanently signed in as the
# admin. Runs unprivileged, so the pid file, logs, and buffer paths live
# under the state dir. proxy_request_buffering off + client_max_body_size 0
# stream git pushes of any size straight through.
STAMP_USER=$(sed -n 's/.*"username": "\([^"]*\)".*/\1/p' "$CREDS_DIR/admin_credentials.json")
as_user mkdir -p "$ROOT/nginx/tmp" "$ROOT/log"
as_user tee "$NGINX_CONF" >/dev/null << EOF
pid $ROOT/nginx/nginx.pid;
error_log $ROOT/nginx/error.log;
worker_processes 1;

events {
    worker_connections 128;
}

http {
    access_log off;
    client_body_temp_path $ROOT/nginx/tmp/client_body;
    proxy_temp_path $ROOT/nginx/tmp/proxy;
    fastcgi_temp_path $ROOT/nginx/tmp/fastcgi;
    uwsgi_temp_path $ROOT/nginx/tmp/uwsgi;
    scgi_temp_path $ROOT/nginx/tmp/scgi;
    client_max_body_size 0;

    server {
        listen $WEB_PORT;

        location / {
            proxy_pass http://127.0.0.1:$BACKEND_PORT;
            proxy_set_header X-WEBAUTH-USER $STAMP_USER;
            proxy_set_header Host \$http_host;
            proxy_http_version 1.1;
            proxy_request_buffering off;
            proxy_read_timeout 300s;
        }
    }
}
EOF

# ---- Serve ---------------------------------------------------------------
as_user gitea web --config "$APP_INI" --work-path "$ROOT" \
  >> "$ROOT/log/launch.log" 2>&1 &

for _ in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:$BACKEND_PORT/api/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if ! curl -fsS "http://127.0.0.1:$BACKEND_PORT/api/healthz" >/dev/null 2>&1; then
  echo "gitea did not become healthy within 30s; see $ROOT/log/launch.log" >&2
  exit 1
fi

as_user nginx -c "$NGINX_CONF" -g "daemon off;" &

# Exit when either server dies, handing recovery to the restart policy.
wait -n
exit 1
