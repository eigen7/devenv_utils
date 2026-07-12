"""DevTool: project dev-workflow helpers (C++ formatting).

Construct one from a DevenvConfig -- it uses config.repo_root to resolve
paths -- and call it from a project's thin py/tools wrappers. All paths are
interpreted relative to the configured repo root, and all subprocesses run
with that directory as cwd.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .config import DevenvConfig

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


def _clang_format_available() -> bool:
    return subprocess.run(
        ["clang-format", "--version"], capture_output=True
    ).returncode == 0


def _clang_format_clean(path: str) -> bool:
    """Return True if *path* already matches clang-format's output."""
    return subprocess.run(
        ["clang-format", "--dry-run", "--Werror", path], capture_output=True
    ).returncode == 0
