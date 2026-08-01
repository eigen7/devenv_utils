"""GitHub access for the dev workflow: one token, shared by host and container.

The user provisions a GitHub token once, through the wizard step
`SetupWizardTool.setup_github_access()`; it is stored at ~/.devenv/github_token
on the host and bind-mounted read-only into every dev container. Inside the
container the entrypoint wires it into git (a credential helper for
https://github.com) and into token-reading tools (GH_TOKEN), so pushing a
branch and opening a PR need no further setup.

The recommended token is a fine-grained personal access token of a dedicated
machine account, granted Contents and Pull-requests read/write on only the
repos this machine works on: the container can then push feature branches and
open PRs, and nothing else. Keeping `main` un-pushable from the container is
prepush_guard.py's job (plus branch protection on repos that support it).
"""

import getpass
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .config import DevenvConfig
from .console import SetupException, print_green, print_red
from .state import in_docker_container

DEVENV_UTILS_REPO_URL = "https://github.com/eigen7/devenv_utils.git"

HOST_TOKEN_PATH = Path.home() / ".devenv" / "github_token"
CONTAINER_TOKEN_PATH = Path("/workspace/github-token")

API_BASE = "https://api.github.com"


def token_path() -> Path:
    return CONTAINER_TOKEN_PATH if in_docker_container() else HOST_TOKEN_PATH


def read_token() -> str:
    path = token_path()
    try:
        return path.read_text().strip()
    except OSError:
        raise SetupException(
            f"No GitHub token at {path}.\n"
            "Run ./setup_wizard.py on the host (the setup_github_access step), then\n"
            "relaunch the container so the token mount picks it up."
        ) from None


def api(method: str, path: str, body: dict | None = None):
    """Call the GitHub REST API with the provisioned token; returns parsed JSON.

    Raises SetupException with GitHub's error payload on failure -- the payload
    names the offending field (e.g. a PR that already exists), which is more
    useful than the bare status code.
    """
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {read_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "devenv-utils",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SetupException(f"GitHub API {method} {path} failed ({e.code}):\n{detail}") from None
    except urllib.error.URLError as e:
        raise SetupException(f"Could not reach {API_BASE}: {e.reason}") from None


def origin_repo(repo_root: Path) -> str:
    """The 'owner/repo' slug of `repo_root`'s origin remote."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for prefix in ("https://github.com/", "git@github.com:"):
        if url.startswith(prefix):
            return url.removeprefix(prefix).removesuffix(".git")
    raise SetupException(f"origin remote {url!r} is not a github.com URL.")


def find_open_pr(slug: str, branch: str) -> dict | None:
    """The open PR for `branch` on `slug`, if one exists (head filtering needs
    the owner-qualified form)."""
    owner = slug.split("/")[0]
    prs = api("GET", f"/repos/{slug}/pulls?state=open&head={owner}:{branch}")
    return prs[0] if prs else None


def open_pr(slug: str, *, head: str, base: str, title: str, body: str) -> dict:
    return api(
        "POST",
        f"/repos/{slug}/pulls",
        {"title": title, "head": head, "base": base, "body": body},
    )


def dev_container_args() -> list[str]:
    """`docker run` args mounting the token into a dev container. The wizard
    provisions the token before the first launch, so its absence means an
    incomplete setup."""
    if not HOST_TOKEN_PATH.exists():
        raise SetupException(f"No GitHub token at {HOST_TOKEN_PATH}. Run ./setup_wizard.py first.")
    return ["-v", f"{HOST_TOKEN_PATH}:{CONTAINER_TOKEN_PATH}:ro"]


def _token_login(token: str) -> str | None:
    """The login the token authenticates as, or None if GitHub rejects it."""
    request = urllib.request.Request(
        f"{API_BASE}/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "devenv-utils",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)["login"]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError):
        return None


def wizard_setup(config: DevenvConfig):
    """Interactive provisioning of the GitHub token (wizard step body).

    Re-runs are quiet when the stored token still authenticates; a missing or
    revoked token prompts for a fresh one. Stored mode 600 -- it is a
    credential, and the container mounts it read-only.
    """
    if HOST_TOKEN_PATH.exists():
        login = _token_login(HOST_TOKEN_PATH.read_text().strip())
        if login is not None:
            print_green(f"GitHub token OK (authenticates as {login}).")
            return
        print_red(f"The token at {HOST_TOKEN_PATH} no longer authenticates; replacing it.")

    print("The dev container pushes feature branches and opens pull requests on")
    print("github.com, authenticating with a token mounted in from the host.")
    print("Recommended: a fine-grained personal access token of a dedicated")
    print("machine account, granted Contents + Pull requests (read/write) on only")
    print(f"the repos this machine works on (at minimum: {origin_repo(config.repo_root)}).")
    while True:
        token = getpass.getpass("GitHub token (input hidden): ").strip()
        if not token:
            continue
        login = _token_login(token)
        if login is None:
            print_red("GitHub rejected that token; try again (Ctrl-C to abort setup).")
            continue
        break

    HOST_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOST_TOKEN_PATH.write_text(token + "\n")
    HOST_TOKEN_PATH.chmod(0o600)
    print_green(f"GitHub token stored at {HOST_TOKEN_PATH} (authenticates as {login}).")
