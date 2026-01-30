"""Dockerfile linting using hadolint."""

import platform
import subprocess
import sys
from pathlib import Path


def get_bin_path() -> Path:
    """Get the path to the bin directory for the current platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin" and machine == "arm64":
        platform_dir = "darwin-arm64"
    elif system == "linux" and machine in ("x86_64", "amd64"):
        platform_dir = "linux-amd64"
    elif system == "linux" and machine in ("arm64", "aarch64"):
        platform_dir = "linux-arm64"
    else:
        raise RuntimeError(f"Unsupported platform: {system}-{machine}")

    bin_path = Path(__file__).parent.parent / "bin" / platform_dir
    if not bin_path.exists():
        raise RuntimeError(f"Bin directory not found: {bin_path}")
    return bin_path


def get_hadolint_path() -> Path:
    """Get the path to the hadolint binary."""
    binary = get_bin_path() / "hadolint"
    if not binary.exists():
        raise RuntimeError(f"hadolint binary not found: {binary}")
    return binary


def run_lint(
    image_ref: str,
    format: str = "tty",
    strict: bool = False,
) -> int:
    """Run hadolint on a Dockerfile.

    Args:
        image_ref: Image reference in format 'name:tag'
        format: Output format (tty, json, checkstyle, sarif)
        strict: Treat warnings as errors

    Returns:
        Exit code from hadolint (0 = pass, 1 = lint errors)
    """
    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}', expected format: name:tag")

    name, tag = image_ref.split(":", 1)
    dockerfile_path = Path("dist") / name / tag / "Dockerfile"

    if not dockerfile_path.exists():
        print(f"Error: Dockerfile not found: {dockerfile_path}", file=sys.stderr)
        print(f"Run 'image-manager generate' first.", file=sys.stderr)
        return 1

    hadolint = get_hadolint_path()
    cmd = [str(hadolint)]

    # Add format option
    if format != "tty":
        cmd.extend(["--format", format])

    # Add strict mode (fail on warnings)
    if strict:
        cmd.extend(["--failure-threshold", "warning"])

    cmd.append(str(dockerfile_path))

    # Flush stdout before running to ensure proper output ordering
    print(f"Linting {image_ref}...", flush=True)
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"  No issues found")

    return result.returncode
