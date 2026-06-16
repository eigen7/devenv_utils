"""DevTool: project dev-workflow helpers (C++ formatting, git-subtree management).

Construct one from a DevenvConfig -- it uses config.repo_root to resolve paths
and config.subtrees to drive the subtree helpers -- and call it from a
project's thin py/tools wrappers. All paths are interpreted relative to the
configured repo root, and all subprocesses run with that directory as cwd.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .config import DevenvConfig, SubtreeSpec

CPP_EXTENSIONS = {".cpp", ".h", ".inl", ".hpp", ".cc", ".cxx"}


def _abort(message: str) -> None:
    """Print an error to stderr and exit non-zero."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


class DevTool:
    """Dev-workflow actions for one project, bound to its DevenvConfig."""

    def __init__(self, config: DevenvConfig):
        self._config = config

    @property
    def repo_root(self) -> Path:
        return self._config.repo_root

    # -- git hooks ----------------------------------------------------------

    def ensure_git_hooks(self) -> None:
        """Activate the vendored pre-commit guard for this checkout.

        Points the repo's `core.hooksPath` at this subtree's `hooks/` dir, so
        the read-only-subtree guard runs without any per-developer setup. The
        path is computed relative to the repo root, so it works at whatever
        prefix the subtree is vendored under. Because `.git` is bind-mounted
        into the dev container, this one setting covers git run on both the host
        and inside the container. Idempotent; a no-op outside a git checkout.
        """
        if not (self.repo_root / ".git").exists():
            return
        hooks_dir = Path(__file__).resolve().parent / "hooks"
        rel = hooks_dir.relative_to(self.repo_root)
        subprocess.run(["git", "config", "core.hooksPath", str(rel)],
                       cwd=self.repo_root, check=False)

    # -- clang-format -------------------------------------------------------

    def clang_format_cli(self, cpp_dirs: list[str]) -> None:
        """Parse argv and run clang_format_all_cpp_files over *cpp_dirs*.

        Thin command-line front-end: ``--check`` selects check-only mode,
        otherwise files are reformatted in place.
        """
        parser = argparse.ArgumentParser(
            description="Run clang-format over the project's C++ sources. With "
                        "--check, report files that would change and exit "
                        "non-zero if any do; otherwise reformat in place.",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="check formatting without modifying files (exit 1 if any differ)",
        )
        args = parser.parse_args()
        self.clang_format_all_cpp_files(cpp_dirs, check=args.check)

    def clang_format_all_cpp_files(self, cpp_dirs: list[str], *, check: bool = False) -> None:
        """Run clang-format over every C++ file under each of *cpp_dirs*.

        Directories are resolved relative to the repo root. With check=True,
        report files that would change and exit non-zero if any do; otherwise
        reformat in place.
        """
        if not _clang_format_available():
            _abort("clang-format not found on PATH.")
        files = self._find_cpp_files(cpp_dirs)
        label = ", ".join(cpp_dirs)
        if not files:
            print(f"No C++ files found under {label}.")
            return
        print(f"Found {len(files)} C++ file(s) under {label}.")
        if check:
            self._clang_format_check(files)
        else:
            self._clang_format_in_place(files)

    def _find_cpp_files(self, cpp_dirs: list[str]) -> list[str]:
        """Return the sorted C++ source paths under each directory in *cpp_dirs*."""
        files = []
        for rel_dir in cpp_dirs:
            for dirpath, _, names in os.walk(self.repo_root / rel_dir):
                for name in names:
                    if os.path.splitext(name)[1] in CPP_EXTENSIONS:
                        files.append(os.path.join(dirpath, name))
        files.sort()
        return files

    def _clang_format_check(self, files: list[str]) -> None:
        """Report files that clang-format would change; exit 1 if any do."""
        bad = [os.path.relpath(p, self.repo_root)
               for p in files if not _clang_format_clean(p)]
        if bad:
            print(f"{len(bad)} file(s) need formatting:")
            for f in bad:
                print(f"  {f}")
            sys.exit(1)
        print("All files are correctly formatted.")

    def _clang_format_in_place(self, files: list[str]) -> None:
        """Reformat each file in place."""
        for path in files:
            subprocess.run(["clang-format", "-i", path], check=True)
            print(f"  formatted {os.path.relpath(path, self.repo_root)}")
        print("Done.")

    # -- git subtrees -------------------------------------------------------

    def pull_git_subtrees_cli(self, subtrees_root: str) -> None:
        """Parse argv (``-y``/``--yes``) and pull every subtree."""
        assume_yes = _parse_subtree_yes(
            "Pull each git subtree under the subtrees root to its upstream tip. "
            "Refuses to run if anything under that root has uncommitted changes, "
            "since `git subtree pull` merges into the subtree prefix.",
            "pull",
        )
        self.pull_git_subtrees(subtrees_root, assume_yes=assume_yes)

    def push_git_subtrees_cli(self, subtrees_root: str) -> None:
        """Parse argv (``-y``/``--yes``) and push every subtree."""
        assume_yes = _parse_subtree_yes(
            "Push each git subtree under the subtrees root to its upstream "
            "branch. Unlike pulling, this does not require a clean working tree.",
            "push",
        )
        self.push_git_subtrees(subtrees_root, assume_yes=assume_yes)

    def pull_git_subtrees(self, subtrees_root: str, *, assume_yes: bool = False) -> None:
        """Pull each declared subtree under *subtrees_root* to its upstream tip.

        `git subtree pull` merges into the subtree prefix, so it only needs that
        prefix to be clean; uncommitted changes elsewhere in the working tree
        are left untouched. Refuses to run if anything under *subtrees_root* has
        staged or unstaged changes. Prompts before each subtree unless
        assume_yes is set.
        """
        if not self._path_is_clean(subtrees_root):
            _abort(f"{subtrees_root}/ has uncommitted changes. Commit or stash "
                   "them before pulling subtrees.")
        self._for_each_subtree(subtrees_root, "Pull", assume_yes, self._pull_one)

    def push_git_subtrees(self, subtrees_root: str, *, assume_yes: bool = False) -> None:
        """Push each declared subtree under *subtrees_root* to its upstream branch.

        Unlike pulling, this does not require a clean working tree. Prompts
        before each subtree unless assume_yes is set.
        """
        self._for_each_subtree(subtrees_root, "Push", assume_yes, self._push_one)

    def _for_each_subtree(self, subtrees_root, verb, assume_yes, op) -> None:
        """Validate config against disk, run *op* per declared subtree, summarize."""
        specs = self._config.subtrees
        if not specs:
            print("No subtrees declared in config.subtrees.")
            return
        self._validate_subtrees_on_disk(subtrees_root, specs)

        results = []
        for spec in specs:
            prefix = f"{subtrees_root}/{spec.name}"
            if not assume_yes and not _confirm(
                f"{verb} {prefix} from {spec.url} ({spec.branch})?"
            ):
                print(f"  Skipping {prefix}.")
                results.append((prefix, "skipped"))
                continue
            ok = op(prefix, spec)
            results.append((prefix, "ok" if ok else "FAILED"))

        _print_summary(results)
        if any(status == "FAILED" for _, status in results):
            sys.exit(1)

    def _validate_subtrees_on_disk(self, subtrees_root: str, specs: list[SubtreeSpec]) -> None:
        """Cross-check declared subtrees against the directories on disk.

        Warns about subtree directories with no config entry, and aborts if a
        declared subtree has no directory (nothing to pull or push into).
        """
        root = self.repo_root / subtrees_root
        if not root.is_dir():
            _abort(f"subtrees dir not found at {root}")
        on_disk = {p.name for p in root.iterdir()
                   if p.is_dir() and not p.name.startswith(("_", "."))}
        declared = {s.name for s in specs}
        for name in sorted(on_disk - declared):
            print(f"WARNING: {subtrees_root}/{name}/ has no entry in "
                  "config.subtrees; skipping it.")
        for name in sorted(declared - on_disk):
            _abort(f"declared subtree '{name}' has no directory at "
                   f"{subtrees_root}/{name}/")

    def _pull_one(self, prefix: str, spec: SubtreeSpec) -> bool:
        return self._git_subtree(
            ["pull", f"--prefix={prefix}", spec.url, spec.branch, "--squash"]
        )

    def _push_one(self, prefix: str, spec: SubtreeSpec) -> bool:
        return self._git_subtree(
            ["push", f"--prefix={prefix}", spec.url, spec.branch]
        )

    def _git_subtree(self, args: list[str]) -> bool:
        """Run `git subtree <args>` from the repo root. Return True on success."""
        cmd = ["git", "subtree", *args]
        print(f"\n$ {' '.join(cmd)}")
        return subprocess.run(cmd, cwd=self.repo_root).returncode == 0

    def _path_is_clean(self, rel_path: str) -> bool:
        """Return True if *rel_path* has no staged or unstaged changes.

        Scoped to the given path so changes elsewhere in the working tree do
        not block subtree operations.
        """
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", rel_path],
            capture_output=True, text=True, cwd=self.repo_root,
        )
        return result.returncode == 0 and result.stdout.strip() == ""


def _clang_format_available() -> bool:
    return subprocess.run(
        ["clang-format", "--version"], capture_output=True
    ).returncode == 0


def _clang_format_clean(path: str) -> bool:
    """Return True if *path* already matches clang-format's output."""
    return subprocess.run(
        ["clang-format", "--dry-run", "--Werror", path], capture_output=True
    ).returncode == 0


def _parse_subtree_yes(description: str, verb: str) -> bool:
    """Parse the shared ``-y``/``--yes`` flag for a subtree CLI; return its value."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help=f"skip confirmation prompts and {verb} all subtrees",
    )
    return parser.parse_args().yes


def _confirm(question: str) -> bool:
    """Prompt with a [Y/n] question, defaulting to yes on empty input."""
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def _print_summary(results: list) -> None:
    print("\n" + "=" * 50)
    print("Summary")
    print("=" * 50)
    for prefix, status in results:
        print(f"  {prefix}: {status}")
