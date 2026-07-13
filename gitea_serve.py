"""Provision (on first run) and launch the local Gitea stack used for PR review.

Gitea gives worktree branches a GitHub-style pull-request UI -- diffs, review
comments, "changes since last review" -- while keeping everything on-machine:
the stack runs inside the dev container, its state lives under
<mount>/gitea, and the host browser reaches it at http://localhost:<port>/
through the port published by the project's run_docker.py.

One stack serves every consumer project (they share the mount): each project
registers its own server-side repo, named after DevenvConfig.name. Consumer
repos expose this module through a thin py/tools/gitea_serve.py shim that
passes their DevenvConfig.

There is no login step, ever: nginx fronts Gitea on the published port and
stamps every request with the reverse-proxy auth header, so the browser is
permanently signed in as the admin user. Anyone who can reach the port is
that user, which is why the port should be published bound to 127.0.0.1 on
the host rather than 0.0.0.0. Gitea itself listens loopback-only on
<port>+1; the `gitea` remote targets it directly over basic auth, so git
operations work independently of nginx.

ensure_serving() is idempotent; call it whenever the server is needed:

  1. First run: writes app.ini and nginx.conf, initializes the sqlite
     database, and creates the admin user with a generated password stored
     (mode 600) in <mount>/gitea/admin_credentials.json. The web UI never
     asks for it; git over http uses it via the remote URL.
  2. Starts `gitea web` and nginx in the background unless already serving.
  3. Ensures the consumer repo has a `gitea` remote and pushes main once so
     the server-side repo exists as a base for pull requests.

To reset the instance entirely, stop the servers (`pkill -x gitea`,
`pkill -x nginx`) and delete <mount>/gitea; the next run re-provisions from
scratch.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DevenvConfig
from .instances import INSTANCE_PORT_OFFSET_ENV
from .stale_worktrees import print_stale_report

GITEA_ROOT = Path("/workspace/mount/gitea")
APP_INI = GITEA_ROOT / "app.ini"
DB_PATH = GITEA_ROOT / "data" / "gitea.db"
CREDENTIALS_PATH = GITEA_ROOT / "admin_credentials.json"
LAUNCH_LOG = GITEA_ROOT / "log" / "launch.log"
NGINX_DIR = GITEA_ROOT / "nginx"
NGINX_CONF = NGINX_DIR / "nginx.conf"

DEFAULT_PORT = 3000
REMOTE_NAME = "gitea"

SERVER_START_TIMEOUT_S = 30

# Gitea binds loopback-only: the outside world comes in through nginx, and the
# reverse-proxy auth header must not be spoofable from beyond the container.
# ENABLE_PUSH_CREATE_USER lets the initial `git push` create the server-side
# repo, so provisioning needs no API calls; the repo is created public
# (DEFAULT_PUSH_CREATE_PRIVATE = false) so API reads need no auth.
# Year-long sessions keep any non-header auth state (e.g. API tokens created
# later) from expiring under a single user.
APP_INI_TEMPLATE = """\
APP_NAME = Dev Forge
RUN_MODE = prod
WORK_PATH = {root}

[server]
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {backend_port}
ROOT_URL = http://localhost:{web_port}/
DISABLE_SSH = true
OFFLINE_MODE = true

[database]
DB_TYPE = sqlite3
PATH = {root}/data/gitea.db

[repository]
ROOT = {root}/repos
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
ROOT_PATH = {root}/log

[session]
PROVIDER = file
SESSION_LIFE_TIME = 31536000

[actions]
ENABLED = false

[cron.update_checker]
ENABLED = false
"""

# Runs as the unprivileged dev user, so the pid file, logs, and every buffer
# path live under the mount instead of nginx's root-owned defaults.
# proxy_request_buffering off + client_max_body_size 0 stream git pushes of
# any size straight through to Gitea.
NGINX_CONF_TEMPLATE = """\
pid {root}/nginx/nginx.pid;
error_log {root}/nginx/error.log;
worker_processes 1;

events {{
    worker_connections 128;
}}

http {{
    access_log off;
    client_body_temp_path {root}/nginx/tmp/client_body;
    proxy_temp_path {root}/nginx/tmp/proxy;
    fastcgi_temp_path {root}/nginx/tmp/fastcgi;
    uwsgi_temp_path {root}/nginx/tmp/uwsgi;
    scgi_temp_path {root}/nginx/tmp/scgi;
    client_max_body_size 0;

    server {{
        listen {web_port};

        location / {{
            proxy_pass http://127.0.0.1:{backend_port};
            proxy_set_header X-WEBAUTH-USER {admin_user};
            proxy_set_header Host $http_host;
            proxy_http_version 1.1;
            proxy_request_buffering off;
            proxy_read_timeout 300s;
        }}
    }}
}}
"""


def instance_port_offset() -> int:
    """This container's port offset (0 if unset), so parallel instances
    launched from sibling repo clones bind non-colliding ports."""
    try:
        offset = int(os.environ.get(INSTANCE_PORT_OFFSET_ENV, ""))
    except ValueError:
        return 0
    return offset if offset >= 0 else 0


def gitea_cmd(*args: str) -> list[str]:
    return ["gitea", *args, "--config", str(APP_INI), "--work-path", str(GITEA_ROOT)]


def gitea_env() -> dict[str, str]:
    """This process's environment minus GIT_CONFIG_* overrides.

    Agent harnesses (e.g. Claude Code) inject GIT_CONFIG_COUNT/KEY_n/VALUE_n
    settings such as safe.bareRepository=explicit into their shells. Gitea's
    internal git commands operate on its own bare repos and fail under that
    override, so it must not leak into the server or CLI environment.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}


def run_gitea(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(gitea_cmd(*args), capture_output=True, text=True, env=gitea_env())


def http_get_status(url: str) -> int | None:
    """The status code of a GET to `url`, or None if the server is unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return None


def gitea_running(backend_port: int) -> bool:
    return http_get_status(f"http://127.0.0.1:{backend_port}/api/healthz") == 200


def nginx_running(web_port: int) -> bool:
    return http_get_status(f"http://127.0.0.1:{web_port}/") is not None


def wait_until(condition, what: str):
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.25)
    raise SystemExit(f"{what} did not come up within {SERVER_START_TIMEOUT_S}s; see {LAUNCH_LOG}")


def git_user_email(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "config", "user.email"], capture_output=True, text=True, cwd=repo_root
    )
    return result.stdout.strip() or "dev@localhost"


def admin_username(repo_root: Path) -> str:
    """The Gitea admin username, derived from the git identity's email.

    The username is part of every Gitea URL (the repos live under it), so it
    is personalized rather than fixed. Gitea usernames may contain letters,
    digits, and ``.-_`` only; anything else in the email's local part is
    dropped.
    """
    local_part = git_user_email(repo_root).split("@")[0]
    sanitized = re.sub(r"[^0-9A-Za-z._-]", "", local_part)
    return sanitized or "dev"


def init_database():
    result = run_gitea("migrate")
    if result.returncode != 0:
        raise SystemExit(f"gitea migrate failed:\n{result.stderr}")


def create_admin_user(repo_root: Path) -> dict:
    creds = {"username": admin_username(repo_root), "password": secrets.token_urlsafe(16)}
    result = run_gitea(
        "admin",
        "user",
        "create",
        "--admin",
        "--username",
        creds["username"],
        "--password",
        creds["password"],
        "--email",
        git_user_email(repo_root),
        "--must-change-password=false",
    )
    if result.returncode != 0:
        raise SystemExit(f"gitea admin user create failed:\n{result.stderr}")
    CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2) + "\n")
    CREDENTIALS_PATH.chmod(0o600)
    return creds


def write_app_ini(web_port: int, backend_port: int):
    """Write app.ini if absent (Gitea appends generated secrets to it on first
    start, so an existing file is never overwritten)."""
    if APP_INI.exists():
        return
    GITEA_ROOT.mkdir(parents=True, exist_ok=True)
    APP_INI.write_text(
        APP_INI_TEMPLATE.format(root=GITEA_ROOT, web_port=web_port, backend_port=backend_port)
    )


def write_nginx_conf(web_port: int, backend_port: int, admin_user: str):
    """Write nginx.conf if absent; it stamps every request with `admin_user`."""
    if NGINX_CONF.exists():
        return
    (NGINX_DIR / "tmp").mkdir(parents=True, exist_ok=True)
    NGINX_CONF.write_text(
        NGINX_CONF_TEMPLATE.format(
            root=GITEA_ROOT,
            web_port=web_port,
            backend_port=backend_port,
            admin_user=admin_user,
        )
    )


def provision(repo_root: Path, web_port: int, backend_port: int) -> dict:
    """One-time instance setup (configs, database, admin user); returns admin credentials."""
    write_app_ini(web_port, backend_port)
    if not DB_PATH.exists():
        init_database()
    if CREDENTIALS_PATH.exists():
        creds = json.loads(CREDENTIALS_PATH.read_text())
    else:
        creds = create_admin_user(repo_root)
    # After credential resolution, so the stamped username always matches the
    # admin user actually in the database (not just the current git identity).
    write_nginx_conf(web_port, backend_port, creds["username"])
    return creds


def start_gitea(backend_port: int):
    LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_LOG.open("a") as log:
        subprocess.Popen(
            gitea_cmd("web"),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=gitea_env(),
        )
    wait_until(lambda: gitea_running(backend_port), "Gitea")


def start_nginx(web_port: int):
    result = subprocess.run(["nginx", "-c", str(NGINX_CONF)], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"nginx failed to start:\n{result.stderr}")
    wait_until(lambda: nginx_running(web_port), "nginx")


def remote_url(cfg: DevenvConfig, creds: dict, backend_port: int) -> str:
    return (
        f"http://{creds['username']}:{creds['password']}@localhost:{backend_port}"
        f"/{creds['username']}/{cfg.name}.git"
    )


def ensure_remote(cfg: DevenvConfig, url: str):
    result = subprocess.run(
        ["git", "remote", "get-url", REMOTE_NAME],
        capture_output=True,
        text=True,
        cwd=cfg.repo_root,
    )
    if result.returncode != 0:
        subprocess.run(["git", "remote", "add", REMOTE_NAME, url], check=True, cwd=cfg.repo_root)
    elif result.stdout.strip() != url:
        subprocess.run(
            ["git", "remote", "set-url", REMOTE_NAME, url], check=True, cwd=cfg.repo_root
        )


def server_repo_exists(cfg: DevenvConfig, creds: dict, backend_port: int) -> bool:
    url = f"http://127.0.0.1:{backend_port}/api/v1/repos/{creds['username']}/{cfg.name}"
    return http_get_status(url) == 200


def push_main(cfg: DevenvConfig):
    subprocess.run(["git", "push", REMOTE_NAME, "main"], check=True, cwd=cfg.repo_root)


def ensure_serving(cfg: DevenvConfig, web_port: int | None = None) -> tuple[dict, int, int]:
    """Provision (if needed) and start the Gitea stack, and register `cfg`'s
    repo on it, idempotently.

    Returns (admin credentials, web port, backend port) for callers that go on
    to talk to the API or push over git -- e.g. pr_flow.py.
    """
    if web_port is None:
        web_port = DEFAULT_PORT + instance_port_offset()
    backend_port = web_port + 1
    creds = provision(cfg.repo_root, web_port, backend_port)
    if not gitea_running(backend_port):
        start_gitea(backend_port)
    if not nginx_running(web_port):
        start_nginx(web_port)
    ensure_remote(cfg, remote_url(cfg, creds, backend_port))
    if not server_repo_exists(cfg, creds, backend_port):
        push_main(cfg)
    return creds, web_port, backend_port


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"nginx front-end port (default: {DEFAULT_PORT} + instance port offset); "
        "Gitea itself listens on the next port up",
    )
    return parser.parse_args()


def main(cfg: DevenvConfig):
    args = get_args()
    creds, web_port, _ = ensure_serving(cfg, args.port)
    print(f"Gitea:  http://localhost:{web_port}/{creds['username']}/{cfg.name}")
    print(f"Signed in automatically as {creds['username']}; no login needed.")
    print_stale_report(cfg)
