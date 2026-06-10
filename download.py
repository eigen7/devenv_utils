"""Small download helper used to fetch data files at setup time."""

import shutil
import subprocess
from pathlib import Path

from .console import print_red


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def download(url: str, dest: Path) -> bool:
    """Download `url` to `dest` atomically. Prefer curl, fall back to wget."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if have("curl"):
        cmd = ["curl", "-fL", "--retry", "3", "-o", str(tmp), url]
    elif have("wget"):
        cmd = ["wget", "-q", "-O", str(tmp), url]
    else:
        print_red("Need `curl` or `wget` to download files.")
        return False
    print(f"  $ {' '.join(cmd)}")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        if tmp.exists():
            tmp.unlink()
        return False
    tmp.rename(dest)
    return True
