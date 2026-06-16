#!/usr/bin/env python3
"""Scaffold the thin consumer-side glue for a repo that vendors devenv_utils.

Run from the consumer repo root, AFTER adding the subtree:

    git subtree add --prefix=subtrees/devenv_utils \\
        https://github.com/eigen7/devenv_utils.git main --squash
    python3 subtrees/devenv_utils/scaffold_consumer.py

It writes the generic glue (never overwriting an existing file):

    subtrees/__init__.py                      namespace-package marker
    subtrees/README.md                        read-only-subtree docs
    py/setup_check.py                         import bridge for py/ entrypoints
    py/tools/pull_git_subtrees.py             update tool (git subtree pull)
    .github/workflows/subtree-readonly.yml    CI guard
    setup_common.py                           project config TEMPLATE (fill in)

Then: fill in setup_common.py, call `dev_tool().ensure_git_hooks()` once in your
first-run setup (e.g. setup_wizard.py), and run that setup. See CONSUMER_SETUP.md.
"""
import sys
from pathlib import Path

# This script lives at <repo>/subtrees/devenv_utils/scaffold_consumer.py.
REPO_ROOT = Path(__file__).resolve().parents[2]

_FILES = {
    "subtrees/__init__.py": '''\
"""Namespace package for vendored git subtrees (see README.md)."""
''',

    "py/setup_check.py": '''\
"""Import the repo-root `setup_common` from the py/ entrypoints.

In the dev container the py/ scripts run with py/ -- not the repo root -- on
PYTHONPATH, so a plain `import setup_common` fails. Call import_setup_common()
instead; it works on the host too.
"""


def import_setup_common():
    """Import and return the repo-root `setup_common` module."""
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import setup_common
    return setup_common
''',

    "py/tools/pull_git_subtrees.py": '''\
#!/usr/bin/env python3
"""Pull each git subtree under subtrees/ to its upstream tip."""
import sys
from pathlib import Path

# Put py/ on sys.path so `setup_check` resolves when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setup_check import import_setup_common

if __name__ == "__main__":
    import_setup_common().dev_tool().pull_git_subtrees_cli("subtrees")
''',

    ".github/workflows/subtree-readonly.yml": '''\
name: subtree-readonly

# Reject any pushed/PR'd commit that edits a vendored subtree (subtrees/<dir>/).
# Server-side counterpart to the .githooks pre-commit guard -- unbypassable.
# A `git subtree pull` (merge + squash) is ignored: the check walks first-parent
# history.

on:
  pull_request:
  push:
    branches: [main]

jobs:
  readonly:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Check subtrees are read-only
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            base="${{ github.event.pull_request.base.sha }}"
            head="${{ github.event.pull_request.head.sha }}"
          else
            base="${{ github.event.before }}"
            head="${{ github.sha }}"
          fi
          if ! git cat-file -e "${base}^{commit}" 2>/dev/null; then
            base="$(git rev-list --max-parents=0 "$head" | tail -1)"
          fi
          python3 subtrees/devenv_utils/subtree_guard.py "$base" "$head"
''',

    "subtrees/README.md": '''\
This directory contains git subtrees: read-only vendored mirrors of upstream
repos. Each subtree's url/branch is declared in `SUBTREES` in the repo-root
`setup_common.py`.

Update a subtree to its upstream tip with:

```
./py/tools/pull_git_subtrees.py
```

You do not edit a subtree here and you do not push from this checkout. To change
one, edit its own upstream repo, push there, then pull it down. Direct edits are
blocked by `.githooks` (activated by `DevTool.ensure_git_hooks()`) and by the
`subtree-readonly` CI workflow. A `git subtree pull` is exempt (it's a merge).
''',

    "setup_common.py": '''\
"""Project-specific devenv configuration for THIS project.

The generic host-side machinery lives in the `subtrees/devenv_utils` git
subtree; this module supplies the project-specific DevenvConfig and constants.
It lives at the repo root so host-side scripts can import it without PYTHONPATH.
"""

from pathlib import Path

from subtrees.devenv_utils import (
    DevenvConfig,
    DevTool,
    SubtreeSpec,
    check_setup_version as _check_setup_version,
)

REPO_ROOT = Path(__file__).resolve().parent

# Bump to force users to rerun the setup wizard (major bump wipes target/).
SETUP_VERSION = "1.0.0"
# Bump when the Dockerfile changes in a way that requires a rebuild.
MINIMUM_REQUIRED_IMAGE_VERSION = "0.0.0"
# Ports forwarded host -> container by run_docker.py.
REQUIRED_PORTS = []

# Vendored git subtrees. git records neither url nor branch, so declare them.
SUBTREES = [
    SubtreeSpec(name="devenv_utils",
                url="https://github.com/eigen7/devenv_utils.git"),
]


def check_setup_version():
    _check_setup_version(make_config())


def dev_tool() -> DevTool:
    """Return the project's dev-workflow helper (clang-format, git subtrees)."""
    return DevTool(make_config())


def make_config() -> DevenvConfig:
    """Build the DevenvConfig consumed by every host-side script."""
    return DevenvConfig(
        name="CHANGE_ME",
        repo_root=REPO_ROOT,
        required_ports=REQUIRED_PORTS,
        min_image_version=MINIMUM_REQUIRED_IMAGE_VERSION,
        setup_version=SETUP_VERSION,
        subtrees=SUBTREES,
    )
''',
}


def main() -> int:
    wrote, skipped = [], []
    for rel, content in _FILES.items():
        path = REPO_ROOT / rel
        if path.exists():
            skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if rel.endswith(".py") and rel.startswith("py/tools/"):
            path.chmod(0o755)
        wrote.append(rel)

    for rel in wrote:
        print(f"  wrote    {rel}")
    for rel in skipped:
        print(f"  skipped  {rel}  (already exists)")

    print("\nNext steps:")
    print("  1. Fill in setup_common.py (project name, ports, versions).")
    print("  2. Call dev_tool().ensure_git_hooks() once in your first-run setup")
    print("     (e.g. setup_wizard.py) to activate the read-only guard.")
    print("  3. Commit. See subtrees/devenv_utils/CONSUMER_SETUP.md for details.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
