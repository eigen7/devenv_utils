"""NVIDIA driver / Container Toolkit validation for GPU-enabled dev images.

GPU access mode: we prefer CDI (Container Device Interface) over the legacy
`--gpus all` hook when a CDI spec is present on the host. The legacy hook
injects /dev/nvidia* into the container's cgroup behind systemd's back; under
cgroup v2 + the systemd cgroup driver, any later re-evaluation of the scope
(suspend/resume, `systemctl daemon-reload`, ...) regenerates the device filter
without the NVIDIA devices, and CUDA dies inside a running container
("Failed to initialize NVML: Unknown Error") until it is restarted. CDI
declares the devices in the container spec, so they survive re-evaluation.
"""

import re
import subprocess

from .console import SetupException, print_green, print_red, yes_no
from .docker_ops import (
    CDI_SPEC_PATH,
    cdi_spec_exists,
    gpu_docker_args,
    image_exists,
)


def host_driver_version() -> str:
    """The host NVIDIA driver version, or "" if unavailable."""
    try:
        return subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version",
             "--format=csv,noheader"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip().splitlines()[0].strip()
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return ""


# Matches the version suffix of the driver's versioned libcuda (e.g. the
# "580.159.03" in libcuda.so.580.159.03). The two-or-more numeric components
# requirement skips the "libcuda.so.1" soname and "libcuda.so.1::/.../libcuda.so"
# symlink directives that also appear in the spec.
_LIBCUDA_VERSION_RE = re.compile(r"libcuda\.so\.(\d+(?:\.\d+)+)")


def cdi_spec_driver_version() -> str:
    """The driver version recorded in the CDI spec, or "" if undetectable.

    nvidia-ctk references the driver's versioned libcuda (e.g.
    libcuda.so.580.159.03) in the spec; we read the version out of that filename
    rather than depending on a YAML parser.
    """
    if not cdi_spec_exists():
        return ""
    match = _LIBCUDA_VERSION_RE.search(CDI_SPEC_PATH.read_text())
    return match.group(1) if match else ""


def setup_cdi():
    """Interactive step: generate (or refresh) the host's NVIDIA CDI spec.

    Idempotent. Requires sudo for `nvidia-ctk cdi generate`. Skipping is fine:
    launch falls back to `--gpus all`, with the suspend-bug caveat.
    """
    print("GPU access mode: with the legacy `--gpus all` hook, suspend/resume or")
    print("a `systemctl daemon-reload` on the host can silently revoke the GPU")
    print("from a RUNNING container (CUDA fails until you relaunch). The CDI mode")
    print("declares the GPU in the container spec and is immune to this.")
    print()

    driver = host_driver_version()
    if not driver:
        print_red("nvidia-smi unavailable; cannot set up CDI. Fix the driver first.")
        return

    if cdi_spec_exists():
        spec_driver = cdi_spec_driver_version()
        if spec_driver == driver:
            print_green(f"CDI spec {CDI_SPEC_PATH} is present and matches "
                        f"driver {driver}.")
            return
        print(f"CDI spec {CDI_SPEC_PATH} exists but was generated for driver")
        print(f"{spec_driver or 'unknown'}; host driver is {driver}. It should be")
        print("regenerated (a stale spec can reference missing library paths).")
    else:
        print(f"No CDI spec found at {CDI_SPEC_PATH}.")

    if not _have_nvidia_ctk():
        print_red("nvidia-ctk not found; install/update the NVIDIA Container "
                  "Toolkit (>= 1.12) to use CDI.")
        print("Falling back to `--gpus all` (with the suspend-bug caveat).")
        return

    if not yes_no("Generate the CDI spec now (runs nvidia-ctk via sudo)?"):
        print("Skipping. GPU access will use `--gpus all`. Re-run this wizard")
        print("after a driver upgrade or to enable CDI later.")
        return

    result = subprocess.run(
        ["sudo", "nvidia-ctk", "cdi", "generate",
         f"--output={CDI_SPEC_PATH}"],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
    )
    if result.returncode != 0 or not cdi_spec_exists():
        print_red("CDI spec generation failed:")
        print(result.stderr)
        print("Falling back to `--gpus all` (with the suspend-bug caveat).")
        return
    print_green(f"Wrote {CDI_SPEC_PATH} (driver {driver}). Containers launched "
                "from now on are immune to the suspend GPU-loss bug.")


def _have_nvidia_ctk() -> bool:
    return subprocess.run(
        ["nvidia-ctk", "--version"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def validate_nvidia_driver():
    """Confirm the host NVIDIA driver is installed and working."""
    print("Validating NVIDIA driver (nvidia-smi on host)...")
    result = subprocess.run(
        ["nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    if result.returncode == 0:
        print_green("NVIDIA driver is installed and working.")
        return
    print_red("NVIDIA driver validation failed:")
    print(result.stderr)
    print("See NVIDIA's website for driver installation instructions.")
    raise SetupException()


def validate_nvidia_installation(image: str):
    """Confirm the GPU is accessible inside Docker via the Container Toolkit.

    Uses the same GPU args as run_container (CDI if available, else
    `--gpus all`). On failure, first checks the host driver to disambiguate a
    missing driver from a missing/misconfigured NVIDIA Container Toolkit.
    """
    gpu_args = gpu_docker_args()
    mode = "CDI" if gpu_args[0] == "--device" else "--gpus all"
    print(f"Validating GPU access inside Docker (mode: {mode})...")
    if not image_exists(image):
        print_red(f"Image {image} not found; skipping GPU-in-Docker validation.")
        print("Build the image (re-run this wizard) and then re-validate.")
        return
    result = subprocess.run(
        ["docker", "run", "--rm"] + gpu_args + [image, "nvidia-smi"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode == 0:
        print_green(f"GPU is accessible in Docker (mode: {mode}).")
        return

    # Driver present but container toolkit broken (or driver missing).
    validate_nvidia_driver()
    print_red("NVIDIA Container Toolkit validation failed:")
    print(result.stderr)
    print("Install/configure the NVIDIA Container Toolkit:")
    print("  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/"
          "latest/install-guide.html")
    print("Likely applicable sections: 'Installing with Apt' and "
          "'Configuring Docker'.")
    raise SetupException()
