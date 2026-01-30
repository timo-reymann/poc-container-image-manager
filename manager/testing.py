"""Wrapper for container-structure-test binary."""

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import docker
from docker.errors import NotFound, APIError

# Docker-in-Docker container for isolated testing
DIND_CONTAINER_NAME = "image-manager-dind"
DIND_PORT = 2375
DIND_IMAGE = "docker:dind"


def get_host_client() -> docker.DockerClient:
    """Get Docker client for the host daemon."""
    return docker.from_env()


def get_dind_client() -> docker.DockerClient:
    """Get Docker client for the dind daemon."""
    return docker.DockerClient(base_url=f"tcp://127.0.0.1:{DIND_PORT}")


def is_dind_running() -> bool:
    """Check if the dind container is running."""
    try:
        client = get_host_client()
        container = client.containers.get(DIND_CONTAINER_NAME)
        return container.status == "running"
    except NotFound:
        return False
    except Exception:
        return False


def start_dind() -> int:
    """Start Docker-in-Docker container for testing.

    On Linux: Tries to run with minimal capabilities (SYS_ADMIN, NET_ADMIN, MKNOD).
    On macOS: Uses privileged mode as Docker Desktop's VM has cgroup limitations.
    """
    if is_dind_running():
        print("dind container is already running")
        return 0

    client = get_host_client()
    system = platform.system().lower()

    # Remove any existing container
    try:
        old = client.containers.get(DIND_CONTAINER_NAME)
        old.remove(force=True)
    except NotFound:
        pass

    print(f"Starting dind container ({DIND_IMAGE})...")

    if system == "linux":
        # On Linux, try with minimal capabilities first
        try:
            client.containers.run(
                DIND_IMAGE,
                name=DIND_CONTAINER_NAME,
                detach=True,
                cap_add=["SYS_ADMIN", "NET_ADMIN", "MKNOD"],
                security_opt=["seccomp=unconfined", "apparmor=unconfined"],
                ports={"2375/tcp": ("127.0.0.1", DIND_PORT)},
                environment={"DOCKER_TLS_CERTDIR": ""},
                cgroupns="host",
            )
        except APIError as e:
            print(f"Capabilities mode failed ({e}), using privileged mode...")
            try:
                old = client.containers.get(DIND_CONTAINER_NAME)
                old.remove(force=True)
            except NotFound:
                pass

            client.containers.run(
                DIND_IMAGE,
                name=DIND_CONTAINER_NAME,
                detach=True,
                privileged=True,
                ports={"2375/tcp": ("127.0.0.1", DIND_PORT)},
                environment={"DOCKER_TLS_CERTDIR": ""},
            )
    else:
        # On macOS, Docker Desktop requires privileged for dind
        try:
            client.containers.run(
                DIND_IMAGE,
                name=DIND_CONTAINER_NAME,
                detach=True,
                privileged=True,
                ports={"2375/tcp": ("127.0.0.1", DIND_PORT)},
                environment={"DOCKER_TLS_CERTDIR": ""},
            )
        except APIError as e:
            print(f"Failed to start dind container: {e}", file=sys.stderr)
            return 1

    # Wait for Docker daemon inside dind to be ready
    print("Waiting for Docker daemon to be ready...")
    for _ in range(60):  # 30 second timeout
        try:
            dind = get_dind_client()
            dind.ping()
            print(f"dind container started (addr: tcp://127.0.0.1:{DIND_PORT})")
            return 0
        except Exception:
            time.sleep(0.5)

    print("Timeout waiting for dind container", file=sys.stderr)
    return 1


def stop_dind() -> int:
    """Stop the dind container."""
    try:
        client = get_host_client()
        container = client.containers.get(DIND_CONTAINER_NAME)
        container.remove(force=True)
        print("Stopped dind container")
    except NotFound:
        print("dind container was not running")
    except Exception as e:
        print(f"Error stopping dind: {e}", file=sys.stderr)
        return 1
    return 0


def ensure_dind() -> bool:
    """Ensure dind is running, start if needed."""
    if is_dind_running():
        return True
    return start_dind() == 0


def get_docker_host() -> str:
    """Get the Docker host for testing."""
    return f"tcp://127.0.0.1:{DIND_PORT}"


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

    # Find bin directory relative to this file (manager/testing.py -> bin/)
    bin_path = Path(__file__).parent.parent / "bin" / platform_dir
    if not bin_path.exists():
        raise RuntimeError(f"Binary directory not found: {bin_path}")

    return bin_path


def get_container_structure_test_path() -> Path:
    """Get the path to the container-structure-test binary."""
    binary = get_bin_path() / "container-structure-test"
    if not binary.exists():
        raise RuntimeError(f"container-structure-test binary not found: {binary}")
    return binary


def get_dist_path(image_ref: str) -> Path:
    """Get the dist path for an image reference."""
    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}', expected format: name:tag")

    name, tag = image_ref.split(":", 1)
    return Path("dist") / name / tag


def find_test_config(image_ref: str) -> Path:
    """Find the test.yml config for an image reference.

    Args:
        image_ref: Image reference in format 'name:tag' (e.g., 'base:2025.9')

    Returns:
        Path to the test.yml file in dist/
    """
    config_path = get_dist_path(image_ref) / "test.yml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Test config not found: {config_path}\n"
            f"Run 'uv run image-manager' first to generate test configs."
        )

    return config_path


def find_image_tar(image_ref: str) -> Path:
    """Find the image.tar for an image reference.

    Args:
        image_ref: Image reference in format 'name:tag' (e.g., 'base:2025.9')

    Returns:
        Path to the image.tar file in dist/
    """
    tar_path = get_dist_path(image_ref) / "image.tar"

    if not tar_path.exists():
        raise FileNotFoundError(
            f"Image tar not found: {tar_path}\n"
            f"Run 'uv run build-image {image_ref}' first to build the image."
        )

    return tar_path


def load_image_tar(tar_path: Path) -> bool:
    """Load an image tar into the dind Docker daemon.

    Args:
        tar_path: Path to the image tar file

    Returns:
        True if successful, False otherwise
    """
    print(f"Loading image from {tar_path}...")
    try:
        dind = get_dind_client()
        with open(tar_path, "rb") as f:
            images = dind.images.load(f)
        for img in images:
            print(f"Loaded: {img.tags}")
        return True
    except Exception as e:
        print(f"Failed to load image: {e}", file=sys.stderr)
        return False


def pull_image(image_ref: str, snapshot_id: str | None = None) -> bool:
    """Pull an image from the registry into the dind Docker daemon.

    Args:
        image_ref: Image reference in format 'name:tag'
        snapshot_id: Optional snapshot ID suffix for the tag

    Returns:
        True if successful, False otherwise
    """
    from manager.config import get_registry_url, get_push_registry

    registry = get_push_registry()
    name, tag = image_ref.split(":", 1)

    # Apply snapshot suffix if provided
    if snapshot_id:
        tag = f"{tag}-{snapshot_id}"

    full_ref = f"{registry.url}/{name}:{tag}"
    print(f"Pulling image from {full_ref}...")

    try:
        dind = get_dind_client()

        # Configure auth if available
        auth_config = None
        if registry.get_auth():
            username, password = registry.get_auth()
            auth_config = {"username": username, "password": password}

        dind.images.pull(f"{registry.url}/{name}", tag=tag, auth_config=auth_config)
        print(f"Pulled: {full_ref}")

        # Tag as the simple image_ref for container-structure-test
        dind.images.get(full_ref).tag(name, tag.split("-")[0] if snapshot_id else tag)
        return True
    except Exception as e:
        print(f"Failed to pull image: {e}", file=sys.stderr)
        return False


def run_test(
    image_ref: str,
    config_path: Path | None = None,
    auto_start: bool = True,
    pull: bool = False,
    snapshot_id: str | None = None,
) -> int:
    """Run container-structure-test for an image.

    Starts dind container if needed, loads the image (from tar or registry),
    then runs tests.

    Args:
        image_ref: Image reference in format 'name:tag'
        config_path: Optional explicit path to test config. If not provided,
                     will be derived from dist/<name>/<tag>/test.yml
        auto_start: If True, automatically start dind if not running
        pull: If True, pull image from registry instead of loading from tar
        snapshot_id: Optional snapshot ID for registry pull

    Returns:
        Exit code from container-structure-test
    """
    # Ensure dind is running
    if auto_start and not ensure_dind():
        print("Error: Failed to start dind container", file=sys.stderr)
        return 1

    if config_path is None:
        config_path = find_test_config(image_ref)

    # Load image into dind Docker (from registry or tar)
    if pull:
        if not pull_image(image_ref, snapshot_id):
            return 1
    else:
        tar_path = find_image_tar(image_ref)
        if not load_image_tar(tar_path):
            return 1

    binary = get_container_structure_test_path()
    docker_host = get_docker_host()

    cmd = [
        str(binary),
        "test",
        "--image", image_ref,
        "--config", str(config_path),
    ]

    print(f"Running: DOCKER_HOST={docker_host} {' '.join(cmd)}")
    result = subprocess.run(cmd, env={**os.environ, "DOCKER_HOST": docker_host})
    return result.returncode


def main() -> None:
    """CLI entrypoint for test-image command."""
    from manager.cli import CLI

    cli = (
        CLI(
            name="test-image",
            description="Test container images using container-structure-test.\nUses isolated Docker daemon (dind) for testing.",
            daemon_name="dind",
            daemon_addr_fn=get_docker_host,
            is_running_fn=is_dind_running,
            start_fn=start_dind,
            stop_fn=stop_dind,
        )
        .add_option("config", "Test config path (default: dist/<name>/<tag>/test.yml)")
        .add_example("start")
        .add_example("base:2025.9")
        .add_example("dotnet:9.0.300 --config ./custom-test.yml")
        .add_example("stop")
    )

    image_ref, opts = cli.parse_args()

    config_path = Path(opts["config"]) if "config" in opts else None

    try:
        exit_code = run_test(image_ref, config_path)
        sys.exit(exit_code)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
