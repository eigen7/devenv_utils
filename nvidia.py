"""NVIDIA driver / Container Toolkit validation for GPU-enabled dev images."""

import subprocess

from .console import SetupException, print_green, print_red
from .docker_ops import image_exists


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

    Runs `docker run --gpus all <image> nvidia-smi`. On failure, first checks
    the host driver to disambiguate a missing driver from a missing/misconfigured
    NVIDIA Container Toolkit.
    """
    print("Validating NVIDIA Container Toolkit (GPU access inside Docker)...")
    if not image_exists(image):
        print_red(f"Image {image} not found; skipping GPU-in-Docker validation.")
        print("Build the image (re-run this wizard) and then re-validate.")
        return
    result = subprocess.run(
        ["docker", "run", "--rm", "--gpus", "all", image, "nvidia-smi"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode == 0:
        print_green("NVIDIA Container Toolkit works; GPU is accessible in Docker.")
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
