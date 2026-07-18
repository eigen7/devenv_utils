"""Access to the machine-wide Gitea service, from either side of Docker.

The service itself -- a long-lived `devenv-gitea` container managed from the
host -- is described in GITEA.md and driven by gitea_service.py. This module
is the layer everything else talks through: it resolves where the service is
reachable *from the caller's side*, loads the credential files, wraps the
backend API, and owns the URL scheme.

The URL scheme, in short: the `gitea` remote stored in a repo's shared
.git/config is the credential-free, host-shaped canonical URL
(http://localhost:<web_port>/<owner>/<repo>.git). It works on the host as-is;
inside a dev container a system-gitconfig rewrite (written by the container
entrypoint) redirects it to the service container. Python code never relies
on that rewrite: it goes through a GiteaAccess, whose web/backend base URLs
are reachable from the side that constructed it -- container_access() reads
the env contract exported by the dev-container launcher; the host side
builds one from ~/.devenv/gitea.json (see gitea_service.host_access).
"""

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Docker names: the service container (also its DNS name on the network) and
# the user-defined bridge network dev containers share with it.
SERVICE_CONTAINER = "devenv-gitea"
DEVENV_NETWORK = "devenv"

# Fixed ports inside the service container: nginx (auto-admin web front) and
# the Gitea backend (basic auth). The host publishes them loopback-only at
# <web_port> and <web_port>+1.
SERVICE_WEB_PORT = 3000
SERVICE_BACKEND_PORT = 3001
DEFAULT_HOST_WEB_PORT = 3000

# Env contract between the dev-container launcher (gitea_service.py) and
# in-container tooling: the host-side web port (for canonical/browser URLs)
# and the web/backend base URLs reachable from inside the container.
WEB_PORT_ENV = "DEVENV_GITEA_WEB_PORT"
WEB_URL_ENV = "DEVENV_GITEA_WEB_URL"
BACKEND_URL_ENV = "DEVENV_GITEA_BACKEND_URL"

# Read-only bind mount of <state_dir>/credentials in every dev container.
CONTAINER_CREDENTIALS_DIR = Path("/workspace/gitea-credentials")

REMOTE_NAME = "gitea"

CLAUDE_USER = "claude"
# Matches the Claude commit identity, so Gitea links Claude's commits.
CLAUDE_EMAIL = "noreply@anthropic.com"

MISSING_ENV_MESSAGE = (
    "The Gitea service env vars are not set in this container. The container "
    "predates the Gitea service setup: exit it, re-run ./setup_wizard.py on "
    "the host, and relaunch with ./run_docker.py."
)

UNREACHABLE_MESSAGE = (
    "The Gitea service is not reachable. On the host, run "
    f"`docker start {SERVICE_CONTAINER}` (or re-run ./setup_wizard.py if the "
    "service was never provisioned)."
)


@dataclass
class GiteaAccess:
    """One side's view of the Gitea service.

    web_url/backend_url are reachable from the side that built this access;
    host_web_port shapes the canonical remote URLs and the browser URLs
    handed to the user (always host-side, where the browser lives).
    """

    web_url: str
    backend_url: str
    host_web_port: int
    credentials_dir: Path

    # ---- Credentials ----------------------------------------------------

    def admin_creds(self) -> dict:
        return self._creds("admin_credentials.json")

    def claude_creds(self) -> dict:
        return self._creds("claude_credentials.json")

    def _creds(self, filename: str) -> dict:
        path = self.credentials_dir / filename
        if not path.exists():
            raise SystemExit(f"Gitea credential file {path} is missing. {UNREACHABLE_MESSAGE}")
        return json.loads(path.read_text())

    # ---- HTTP -----------------------------------------------------------

    def api(self, method: str, path: str, creds: dict, payload: dict | None = None):
        """A Gitea API call against the backend (plain basic auth, no header
        stamping). Returns the decoded JSON response, or None for empty
        bodies."""
        req = urllib.request.Request(
            f"{self.backend_url}/api/v1{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
        )
        auth = base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
        return json.loads(body) if body else None

    def healthy(self) -> bool:
        return http_get_status(f"{self.backend_url}/api/healthz") == 200

    def ensure_reachable(self):
        if not self.healthy():
            raise SystemExit(UNREACHABLE_MESSAGE)

    def repo_exists(self, owner: str, repo: str) -> bool:
        return http_get_status(f"{self.backend_url}/api/v1/repos/{owner}/{repo}") == 200

    # ---- URLs -----------------------------------------------------------

    def canonical_repo_url(self, owner: str, repo: str) -> str:
        """The credential-free URL stored as the `gitea` remote: host-shaped,
        served by nginx (auto-admin), rewritten inside dev containers by the
        system gitconfig."""
        return f"http://localhost:{self.host_web_port}/{owner}/{repo}.git"

    def browser_url(self, path: str) -> str:
        """A URL for the user's host browser."""
        return f"http://localhost:{self.host_web_port}/{path}"

    def read_repo_url(self, owner: str, repo: str) -> str:
        """An anonymous-read git URL against the backend (repos are public)."""
        return f"{self.backend_url}/{owner}/{repo}.git"

    def authed_repo_url(self, owner: str, repo: str, creds: dict) -> str:
        """A basic-auth git URL against the backend, for pushes attributed to
        `creds`'s user (e.g. claude)."""
        parts = urllib.parse.urlsplit(self.backend_url)
        netloc = f"{creds['username']}:{creds['password']}@{parts.netloc}"
        return f"{parts.scheme}://{netloc}/{owner}/{repo}.git"


def http_get_status(url: str) -> int | None:
    """The status code of a GET to `url`, or None if the server is unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return None


def container_access() -> GiteaAccess:
    """The dev-container side's GiteaAccess, from the launcher's env contract."""
    web_port = os.environ.get(WEB_PORT_ENV)
    web_url = os.environ.get(WEB_URL_ENV)
    backend_url = os.environ.get(BACKEND_URL_ENV)
    if not (web_port and web_url and backend_url):
        raise SystemExit(MISSING_ENV_MESSAGE)
    return GiteaAccess(
        web_url=web_url,
        backend_url=backend_url,
        host_web_port=int(web_port),
        credentials_dir=CONTAINER_CREDENTIALS_DIR,
    )


# ---- Repo enumeration and registration ----------------------------------


def git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def gitmodule_entries(root: Path) -> list[tuple[str, str]]:
    """(name, path) for each submodule declared in root's .gitmodules."""
    listing = subprocess.run(
        ["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    )
    entries = []
    for line in listing.stdout.splitlines():
        key, path = line.split(" ", 1)
        entries.append((key.removeprefix("submodule.").removesuffix(".path"), path))
    return entries


def gitea_repo_name(sub_dir: Path) -> str:
    """A submodule's Gitea repo name -- its origin basename (same project)."""
    return Path(urllib.parse.urlparse(git_out(sub_dir, "remote", "get-url", "origin")).path).stem


def ensure_remote(repo_root: Path, url: str):
    """Point `repo_root`'s `gitea` remote at `url`, adding or updating it."""
    result = subprocess.run(
        ["git", "remote", "get-url", REMOTE_NAME], capture_output=True, text=True, cwd=repo_root
    )
    if result.returncode != 0:
        subprocess.run(["git", "remote", "add", REMOTE_NAME, url], check=True, cwd=repo_root)
    elif result.stdout.strip() != url:
        subprocess.run(["git", "remote", "set-url", REMOTE_NAME, url], check=True, cwd=repo_root)


def ensure_project_remotes(access: GiteaAccess, repo_root: Path, name: str, owner: str):
    """Set the canonical `gitea` remote on a consumer checkout and each of its
    populated submodules (whose server-side repos live under the same owner,
    named after their GitHub origin)."""
    ensure_remote(repo_root, access.canonical_repo_url(owner, name))
    for _, sub_path in gitmodule_entries(repo_root):
        sub = repo_root / sub_path
        if (sub / ".git").exists():
            ensure_remote(sub, access.canonical_repo_url(owner, gitea_repo_name(sub)))


def register_repo(access: GiteaAccess, repo_root: Path, name: str, owner: str):
    """Ensure the consumer repo exists server-side: push main once through the
    canonical remote (push-to-create), acting as the admin via nginx."""
    if access.repo_exists(owner, name):
        return
    subprocess.run(["git", "push", REMOTE_NAME, "main"], check=True, cwd=repo_root)
