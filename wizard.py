"""SetupWizardTool: orchestrates the generic, interactive first-time setup.

Projects subclass this to add project-specific steps (e.g. fetching data
files) and drive the steps from their own setup script. Every generic step is
a method here; project state that flows between steps (notably the chosen mount
directory) is held on the instance.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import DevenvConfig
from .console import SetupException, print_green, print_red, print_rule, yes_no
from .docker_ops import (
    MIN_DOCKER_VERSION,
    build_image,
    docker_server_version,
    is_version_ok,
    major_version,
)
from .gateway_service import wizard_setup as gateway_wizard_setup
from .gitea_service import wizard_setup as gitea_wizard_setup
from .nvidia import setup_cdi, validate_nvidia_driver, validate_nvidia_installation
from .state import get_env_json, is_subpath, update_env_json
from .vscode_attach import (
    vscode_attach_config_paths,
    write_vscode_attach_config,
)


class SetupWizardTool:
    def __init__(self, config: DevenvConfig):
        self.config = config
        self.mount_dir: Path | None = None

    # ---- Step: mount dir ------------------------------------------------

    def setup_mount_dir(self) -> Path:
        c = self.config
        assert c.container_mount_path is not None, (
            f"{c.name} has no container_mount_path; skip the mount-dir step."
        )
        print(f"{c.name} needs a persistent directory on the host that gets")
        print(f"bind-mounted into the Docker container at {c.container_mount_path}.")
        print("It holds data that must outlive any single container, and it MUST")
        print("live outside this repo.")
        print()

        env = get_env_json(c.env_json_path)
        default = env.get("MOUNT_DIR", str(c.default_mount_dir))

        while True:
            ans = input(f"Mount directory [{default}]: ").strip() or default
            target = os.path.abspath(os.path.expanduser(ans))
            if is_subpath(target, c.repo_root):
                print_red(f"Mount dir cannot live inside the repo ({c.repo_root}).")
                continue
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as e:
                print_red(f"Could not create {target}: {e}")
                continue
            break

        update_env_json(c.env_json_path, {"MOUNT_DIR": target})
        print_green(f"Mount dir: {target}")
        self.mount_dir = Path(target)
        return self.mount_dir

    # ---- Step: git config -----------------------------------------------

    # Git hooks installed into the repo's shared hooks directory:
    # hook name -> ordered (devenv_utils script, extra args) entries, run in
    # sequence; a failing entry aborts the hook and skips the rest.
    # prepush_guard.py steers a stray `git push` to GitHub origin back to
    # `git publish`; commit_guard.py keeps direct commits on `main` in
    # lockstep with Gitea; submodule_guard.py blocks backward submodule
    # pointer moves, re-syncs stale submodule checkouts after rebase/merge,
    # and (offer-update, post-merge only) reacts when a pulled-in merge left a
    # submodule's Gitea main ahead of the recorded pointer -- see each
    # script's docstring. offer-update is a network probe, so it runs on
    # post-merge (which fires only when a pull actually merges) but not on
    # post-checkout (which fires on every checkout).
    GIT_HOOKS = {
        "pre-push": [("prepush_guard.py", ' "$@"')],
        "pre-commit": [("commit_guard.py", " pre-commit"), ("submodule_guard.py", " pre-commit")],
        "post-commit": [("commit_guard.py", " post-commit")],
        "post-merge": [
            ("commit_guard.py", " post-merge"),
            ("submodule_guard.py", " sync"),
            ("submodule_guard.py", " offer-update"),
        ],
        "post-checkout": [("submodule_guard.py", " sync")],
    }

    def setup_git_config(self):
        """Apply the git settings the workflow depends on (SUBMODULES.md).

        - submodule.recurse=true: `git pull` / `git checkout` update each
          submodule working tree to match the commit the superproject
          records, so a checkout can't silently go stale.
        - push.recurseSubmodules=check: git refuses to push a commit whose
          submodule pointer references a commit absent from the submodule's
          remote, which would break every other clone.
        - status.submodulesummary=1 / diff.submodule=log: status and diff
          describe a submodule pointer change by the commits it spans (with
          a `(rewind)` marker on backward moves) instead of by raw SHAs.
        - alias.publish: `git publish` runs publish.py (the host-side publish
          step).
        - the GIT_HOOKS guards: the pre-push origin guard, the
          pre/post-commit + post-merge hooks that keep direct `main` commits
          in lockstep with Gitea, and the submodule_guard hooks (backward
          pointer moves blocked at commit time; stale submodule checkouts
          re-synced after rebase/merge).

        Clears core.hooksPath so git uses the repo's default hooks directory,
        where the hooks are installed.
        """
        repo_root = self.config.repo_root
        publish = '!"$(git rev-parse --show-toplevel)"/submodules/devenv_utils/publish.py'
        for key, value in [
            ("submodule.recurse", "true"),
            ("push.recurseSubmodules", "check"),
            ("status.submodulesummary", "1"),
            ("diff.submodule", "log"),
            ("alias.publish", publish),
        ]:
            subprocess.run(["git", "config", key, value], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "--unset", "core.hooksPath"], cwd=repo_root, check=False)
        self._install_git_hooks(repo_root)
        print_green("Configured git for submodule syncing, `git publish`, and the workflow hooks.")

    @classmethod
    def _install_git_hooks(cls, repo_root: Path):
        """Write each GIT_HOOKS entry into the repo's shared hooks directory.

        The hooks directory is shared by every worktree, and each hook resolves
        the devenv_utils script through its own worktree's checkout -- so a
        worktree whose pinned devenv_utils predates a given script must be
        tolerated: the hook no-ops when the script doesn't exist there.
        """
        common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        hooks_dir = Path(common_dir) / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for name, entries in cls.GIT_HOOKS.items():
            lines = ["#!/bin/sh", 'top="$(git rev-parse --show-toplevel)"']
            for script, args in entries:
                lines.append(f'tool="$top/submodules/devenv_utils/{script}"')
                lines.append(f'[ ! -x "$tool" ] || "$tool"{args} || exit $?')
            hook = hooks_dir / name
            hook.write_text("\n".join(lines) + "\n")
            hook.chmod(0o755)

    # ---- Step: Gitea service -------------------------------------------

    def setup_gitea_service(self):
        """Provision (or adopt) the machine-wide Gitea service container and
        register this repo on it. See GITEA.md; call after setup_mount_dir
        (legacy in-mount state detection) and the docker checks."""
        gitea_wizard_setup(self.config)

    # ---- Step: gateway service -----------------------------------------

    def setup_gateway_service(self):
        """Provision the machine-wide gateway (reverse-proxy) service container
        that routes each project's http://<project>-<service>.localhost dev URLs
        to its container ports. See GATEWAY.md."""
        gateway_wizard_setup(self.config)

    # ---- Step: docker permissions --------------------------------------

    def validate_docker_permissions(self):
        print("Checking that you can run `docker` without sudo...")
        result = subprocess.run(
            ["docker", "ps"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            print_green("Docker is usable without sudo.")
            return

        if "permission denied" in result.stderr.lower():
            print_red("You can't run docker without sudo. Add yourself to the docker group:")
            print("    sudo usermod -aG docker $USER")
            print("Then log out and back in.")
        else:
            print_red("`docker ps` failed:")
            print(result.stderr)
        raise SetupException()

    def validate_docker_version(self):
        """Require a Docker daemon new enough to enable CDI by default.

        CDI grants the GPU to the container via the generated
        /etc/cdi/nvidia.yaml spec; Docker enables it by default only from
        MIN_DOCKER_VERSION onward. On older daemons CDI is off, so
        `--device nvidia.com/gpu=all` fails to resolve and GPU access breaks.
        """
        print(f"Checking Docker daemon version (need >= {MIN_DOCKER_VERSION})...")
        version = docker_server_version()
        if is_version_ok(version, MIN_DOCKER_VERSION):
            print_green(f"Docker daemon version {version} is new enough.")
            return
        print_red(
            f"Docker daemon version {version or 'unknown'} is too old; "
            f"need >= {MIN_DOCKER_VERSION}."
        )
        print("CDI (used to grant the GPU to the container) is enabled by default")
        print("only from Docker 28.3.0. Upgrade Docker Engine and re-run:")
        print("    https://docs.docker.com/engine/install/")
        raise SetupException()

    # ---- Step: VS Code attach config -----------------------------------

    def setup_vscode_attach_config(self):
        c = self.config
        paths = vscode_attach_config_paths(c.instance_name)
        if not paths:
            print("No VS Code user-data directory found for any flavor")
            print("(Code / Code - Insiders / VSCodium). Skipping VS Code attach")
            print("config. If you install VS Code later, re-run this wizard, or")
            print("manually use 'Dev Containers: Open Named Container Configuration")
            print(f'File\' and set "remoteUser": "{c.remote_user}".')
            return

        print("VS Code's 'Attach to Running Container' command does NOT read")
        print("this repo's .devcontainer/devcontainer.json. It reads a per-container")
        print("config under the VS Code user-data dir. Without it, vscode-server")
        print("runs as root inside the container.")
        print()
        print("Detected user-data dir(s); proposing to write/merge:")
        for p in paths:
            print(f"  {p}")
        print()
        if not yes_no(f"Configure VS Code attach to run as {c.remote_user}?"):
            print("Skipping VS Code attach config.")
            return

        for p in paths:
            try:
                status = write_vscode_attach_config(
                    p, c.instance_name, c.remote_user, c.container_repo_path
                )
            except RuntimeError as e:
                print_red(str(e))
                continue
            if status == "created":
                print_green(f"Wrote {p}")
            elif status == "updated":
                print_green(f"Updated {p} (merged in remoteUser/workspaceFolder/containerName)")
            else:
                print(f"{p} already up to date.")

    # ---- Step: Claude trust --------------------------------------------

    def setup_claude_trust(self):
        """Pre-trust the container workspace paths in the host Claude config.

        The host ~/.claude.json is bind-mounted into the container, so writing
        trust here avoids an interactive trust prompt when Claude starts inside
        /workspace and /workspace/repo.
        """
        claude_config_path = Path.home() / ".claude.json"
        try:
            if claude_config_path.exists():
                raw = claude_config_path.read_text(encoding="utf-8").strip()
                cfg = json.loads(raw) if raw else {}
                if not isinstance(cfg, dict):
                    raise ValueError("top-level JSON must be an object")
            else:
                cfg = {}
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print_red(f"Could not update {claude_config_path}: {e}")
            return

        projects = cfg.setdefault("projects", {})
        if not isinstance(projects, dict):
            print_red(f"Could not update {claude_config_path}: 'projects' is not an object")
            return

        changed = False
        for trusted_path in ("/workspace", "/workspace/repo"):
            project_cfg = projects.setdefault(trusted_path, {})
            if not isinstance(project_cfg, dict):
                project_cfg = {}
                projects[trusted_path] = project_cfg
            if project_cfg.get("hasTrustDialogAccepted") is not True:
                project_cfg["hasTrustDialogAccepted"] = True
                changed = True

        if not changed:
            print_green("Claude trust already configured for /workspace paths.")
            return

        try:
            claude_config_path.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            print_red(f"Could not write {claude_config_path}: {e}")
            return

        print_green("Configured Claude workspace trust for /workspace and /workspace/repo.")

    # ---- Step: build image ---------------------------------------------

    def build_docker_image(self, context: os.PathLike | None = None):
        c = self.config
        build_image(c.image, context or c.docker_context)

    # ---- Step: NVIDIA --------------------------------------------------

    def validate_nvidia_driver(self):
        validate_nvidia_driver()

    def validate_nvidia_installation(self, image: str | None = None):
        validate_nvidia_installation(image or self.config.image)

    def setup_cdi(self):
        setup_cdi()

    # ---- Setup version --------------------------------------------------

    def rm_target_on_major_bump(self):
        """Remove the build output directory when setup_version's major has
        increased since the last recorded setup, discarding builds made against
        an incompatible contract. Reads the previously committed version, so call
        it before commit() writes the new one."""
        c = self.config
        stored = get_env_json(c.env_json_path).get("SETUP_VERSION")
        if major_version(c.setup_version) <= major_version(stored or ""):
            return
        if c.target_dir.exists():
            print(
                f"Setup version major increased ({stored or 'none'} -> "
                f"{c.setup_version}); removing {c.target_dir} to invalidate "
                f"existing builds."
            )
            shutil.rmtree(c.target_dir)

    def commit(self):
        """Stamp setup_version into .env.json, recording a completed setup.

        Call last, after every step has succeeded: entrypoints can gate on this
        stamp, and rm_target_on_major_bump() compares against it on the next run."""
        update_env_json(self.config.env_json_path, {"SETUP_VERSION": self.config.setup_version})

    # ---- Convenience ----------------------------------------------------

    def rule(self):
        print_rule()


def check_setup_version(config: DevenvConfig) -> None:
    """Exit with guidance to re-run the setup wizard when .env.json's stamp is
    missing or older than config.setup_version.

    Entrypoints call this before doing any real work, so a checkout whose setup
    predates a contract change fails fast with a clear instruction instead of a
    confusing downstream error. SetupWizardTool.commit() writes the stamp this
    reads.
    """
    required = config.setup_version
    stored = get_env_json(config.env_json_path).get("SETUP_VERSION")
    if is_version_ok(stored or "", required):
        return
    reason = (
        "No completed setup was found"
        if not stored
        else f"The recorded setup version {stored} is older than the required {required}"
    )
    print(
        f"\n{'*' * 78}\n"
        f"{reason}.\n"
        f"Please (re-)run the setup wizard on the host, then try again:\n"
        f"    ./setup_wizard.py\n"
        f"{'*' * 78}",
        file=sys.stderr,
    )
    sys.exit(1)
