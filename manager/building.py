"""Wrapper for buildkit (buildctl/buildkitd) binaries."""

import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
from manager.config import get_registry_url, get_registry_auth, get_registries, get_push_registry, get_cache_config, get_labels_config, ConfigLoader
from manager.rendering import generate_tag_report
from docker.errors import NotFound, APIError

# Default socket/config directory in project
DEFAULT_BUILDKIT_DIR = Path(__file__).parent.parent / ".buildkit"
DEFAULT_SOCKET_PATH = DEFAULT_BUILDKIT_DIR / "buildkitd.sock"
CONTAINER_NAME = "image-manager-buildkitd"
CONTAINER_PORT = 8372  # Port for buildkitd on macOS
BUILDKIT_IMAGE = "moby/buildkit:rootless"

# Local registry for base image resolution
REGISTRY_CONTAINER_NAME = "image-manager-registry"
REGISTRY_PORT = 5050  # Using 5050 to avoid conflict with AirPlay on macOS
REGISTRY_IMAGE = "registry:2"

# Garage S3 port for connection checking (local development)
GARAGE_S3_PORT = 3900

# Platform support
SUPPORTED_PLATFORMS = ["linux/amd64", "linux/arm64"]
PLATFORM_ALIASES = {
    "amd64": "linux/amd64",
    "arm64": "linux/arm64",
    "linux/amd64": "linux/amd64",
    "linux/arm64": "linux/arm64",
}
BINFMT_IMAGE = "tonistiigi/binfmt"

# Build reproducibility (2026-01-01 00:00:00 UTC)
SOURCE_DATE_EPOCH = "1767225600"


def get_docker_client() -> docker.DockerClient:
    """Get Docker client for the host daemon."""
    return docker.from_env()


def get_socket_addr() -> str:
    """Get the buildkitd socket address.

    Uses BUILDKIT_HOST env var if set, otherwise:
    - macOS: TCP connection to Docker container
    - Linux: Unix socket
    """
    if addr := os.environ.get("BUILDKIT_HOST"):
        return addr

    if platform.system().lower() == "darwin":
        return f"tcp://127.0.0.1:{CONTAINER_PORT}"

    return f"unix://{DEFAULT_SOCKET_PATH}"


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

    # Find bin directory relative to this file (manager/building.py -> bin/)
    bin_path = Path(__file__).parent.parent / "bin" / platform_dir
    if not bin_path.exists():
        raise RuntimeError(f"Binary directory not found: {bin_path}")

    return bin_path


def get_buildctl_path() -> Path:
    """Get the path to the buildctl binary."""
    binary = get_bin_path() / "buildkit" / "buildctl"
    if not binary.exists():
        raise RuntimeError(f"buildctl binary not found: {binary}")
    return binary


def get_buildkitd_path() -> Path:
    """Get the path to the buildkitd binary (Linux only)."""
    binary = get_bin_path() / "buildkit" / "buildkitd"
    if not binary.exists():
        raise RuntimeError(f"buildkitd binary not found: {binary}")
    return binary


def get_rootlesskit_path() -> Path:
    """Get the path to the rootlesskit binary (Linux only)."""
    binary = get_bin_path() / "rootlesskit"
    if not binary.exists():
        raise RuntimeError(f"rootlesskit binary not found: {binary}")
    return binary


def get_pid_file() -> Path:
    """Get the path to the buildkitd PID file."""
    return DEFAULT_BUILDKIT_DIR / "buildkitd.pid"


def is_container_running() -> bool:
    """Check if buildkitd container is running."""
    try:
        client = get_docker_client()
        container = client.containers.get(CONTAINER_NAME)
        return container.status == "running"
    except NotFound:
        return False
    except Exception:
        return False


def is_buildkitd_running() -> bool:
    """Check if buildkitd is running (native or container)."""
    system = platform.system().lower()

    if system == "darwin":
        return is_container_running()

    # Linux: check PID file
    pid_file = get_pid_file()
    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        pid_file.unlink(missing_ok=True)
        return False


def is_port_open(port: int, timeout: float = 0.1) -> bool:
    """Check if a TCP port is open."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", port))
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def start_buildkitd_container() -> int:
    """Start buildkitd as a rootless Docker container (for macOS)."""
    if is_container_running():
        print("buildkitd container is already running")
        return 0

    client = get_docker_client()

    # Remove any existing container
    try:
        old = client.containers.get(CONTAINER_NAME)
        old.remove(force=True)
    except NotFound:
        pass

    # Create buildkitd config to allow insecure local registry
    # The registry runs on host, accessible via host.docker.internal on macOS
    registry_host = f"host.docker.internal:{REGISTRY_PORT}"
    buildkitd_config = f"""
[registry."{registry_host}"]
  http = true
  insecure = true
"""

    # Create config directory for buildkitd
    config_dir = DEFAULT_BUILDKIT_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "buildkitd.toml"
    config_file.write_text(buildkitd_config)

    print(f"Starting buildkitd container rootless ({BUILDKIT_IMAGE})...")
    try:
        client.containers.run(
            BUILDKIT_IMAGE,
            name=CONTAINER_NAME,
            detach=True,
            # Rootless mode - no privileged flag needed
            security_opt=["seccomp=unconfined", "apparmor=unconfined"],
            ports={f"{CONTAINER_PORT}/tcp": ("127.0.0.1", CONTAINER_PORT)},
            command=[
                "--addr", f"tcp://0.0.0.0:{CONTAINER_PORT}",
                "--oci-worker-no-process-sandbox",
                "--config", "/etc/buildkit/buildkitd.toml",
            ],
            environment={
                "BUILDKITD_FLAGS": "--oci-worker-no-process-sandbox",
            },
            volumes={
                str(config_file.absolute()): {"bind": "/etc/buildkit/buildkitd.toml", "mode": "ro"},
            },
        )
    except APIError as e:
        print(f"Failed to start buildkitd container: {e}", file=sys.stderr)
        return 1

    # Wait for TCP port to be ready
    print("Waiting for buildkitd to be ready...")
    for _ in range(50):  # 5 second timeout
        if is_port_open(CONTAINER_PORT):
            print(f"buildkitd container started rootless (addr: tcp://127.0.0.1:{CONTAINER_PORT})")
            return 0
        time.sleep(0.1)

    print("Warning: buildkitd started but port not yet available", file=sys.stderr)
    return 0


def start_buildkitd_native() -> int:
    """Start buildkitd natively with rootlesskit (Linux only)."""
    if is_buildkitd_running():
        print(f"buildkitd is already running (socket: {DEFAULT_SOCKET_PATH})")
        return 0

    DEFAULT_BUILDKIT_DIR.mkdir(parents=True, exist_ok=True)

    # Create buildkitd config to allow insecure local registry
    registry_host = f"localhost:{REGISTRY_PORT}"
    buildkitd_config = f"""
[registry."{registry_host}"]
  http = true
  insecure = true
"""
    config_dir = DEFAULT_BUILDKIT_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "buildkitd.toml"
    config_file.write_text(buildkitd_config)

    rootlesskit = get_rootlesskit_path()
    buildkitd = get_buildkitd_path()

    # Log file for buildkitd output
    log_file = DEFAULT_BUILDKIT_DIR / "buildkitd.log"

    cmd = [
        str(rootlesskit),
        "--net=host",
        "--copy-up=/etc",
        "--copy-up=/run",
        str(buildkitd),
        "--addr", get_socket_addr(),
        "--root", str(DEFAULT_BUILDKIT_DIR / "root"),
        "--oci-worker-no-process-sandbox",
        "--config", str(config_file),
    ]

    print(f"Starting buildkitd (rootless): {' '.join(cmd)}")

    # Write output to log file so we can debug failures
    with open(log_file, "w") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    get_pid_file().write_text(str(proc.pid))

    # Wait up to 10 seconds for socket to appear
    for _ in range(100):
        if DEFAULT_SOCKET_PATH.exists():
            print(f"buildkitd started rootless (pid: {proc.pid}, socket: {DEFAULT_SOCKET_PATH})")
            return 0
        # Check if process died
        if proc.poll() is not None:
            print(f"Error: buildkitd process exited with code {proc.returncode}", file=sys.stderr)
            if log_file.exists():
                print(f"buildkitd log output:", file=sys.stderr)
                print(log_file.read_text(), file=sys.stderr)
            return 1
        time.sleep(0.1)

    print("Error: buildkitd started but socket not available after 10 seconds", file=sys.stderr)
    if log_file.exists():
        print(f"buildkitd log output:", file=sys.stderr)
        print(log_file.read_text(), file=sys.stderr)
    return 1


def start_buildkitd() -> int:
    """Start buildkitd daemon (container on macOS, native on Linux)."""
    system = platform.system().lower()

    if system == "darwin":
        return start_buildkitd_container()
    elif system == "linux":
        return start_buildkitd_native()
    else:
        print(f"Unsupported platform: {system}", file=sys.stderr)
        return 1


def stop_buildkitd() -> int:
    """Stop buildkitd daemon."""
    system = platform.system().lower()

    if system == "darwin":
        try:
            client = get_docker_client()
            container = client.containers.get(CONTAINER_NAME)
            container.remove(force=True)
            print("Stopped buildkitd container")
        except NotFound:
            print("buildkitd container was not running")
        except Exception as e:
            print(f"Error stopping buildkitd: {e}", file=sys.stderr)
            return 1
        return 0

    # Linux: stop native process
    pid_file = get_pid_file()
    if not pid_file.exists():
        print("buildkitd is not running")
        return 0

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped buildkitd (pid: {pid})")
    except (ValueError, ProcessLookupError):
        print("buildkitd was not running")

    pid_file.unlink(missing_ok=True)
    DEFAULT_SOCKET_PATH.unlink(missing_ok=True)
    return 0


def ensure_buildkitd() -> bool:
    """Ensure buildkitd is running, start if needed.

    If BUILDKIT_HOST env var is set, assumes external buildkitd is available.
    """
    # External buildkitd (e.g., CI service container)
    if os.environ.get("BUILDKIT_HOST"):
        print(f"Using external buildkitd: {os.environ['BUILDKIT_HOST']}")
        return True

    if is_buildkitd_running():
        return True
    return start_buildkitd() == 0


# --- Registry management ---

def get_registry_addr() -> str:
    """Get the registry host for local operations."""
    return get_registry_url()


def is_registry_insecure() -> bool:
    """Check if the push registry should use insecure (HTTP) connections.

    Returns True if the registry is configured as insecure or auto-detected
    as a local registry (localhost, 127.0.0.1, private IPs).
    """
    return get_push_registry().insecure


def get_registry_addr_for_buildkit() -> str:
    """Get the registry host as seen from buildkit container."""
    registry_url = get_registry_url()

    # If it's localhost, buildkit needs host.docker.internal
    if registry_url.startswith("localhost:"):
        port = registry_url.split(":")[1]
        return f"host.docker.internal:{port}"

    return registry_url


def check_registry_connection() -> bool:
    """Check if the registry is reachable."""
    registry_url = get_registry_url()

    # Extract host from URL (may include path like ghcr.io/owner/repo)
    host = registry_url.split("/")[0]

    # Known cloud registries - skip socket check, they use HTTPS
    cloud_registries = [
        "ghcr.io",
        "docker.io",
        "registry.hub.docker.com",
        "gcr.io",
        "us.gcr.io",
        "eu.gcr.io",
        "asia.gcr.io",
        "azurecr.io",
        "ecr.aws",
        "gallery.ecr.aws",
        "quay.io",
    ]

    # Check if host matches or ends with a cloud registry
    for cloud_reg in cloud_registries:
        if host == cloud_reg or host.endswith(f".{cloud_reg}"):
            return True

    # Parse host and port for local/private registries
    if ":" in host:
        host, port_str = host.rsplit(":", 1)
        port = int(port_str)
    else:
        port = 5000  # Default registry port

    try:
        result = socket.create_connection((host, port), timeout=2)
        result.close()
        return True
    except (socket.error, socket.timeout):
        return False


def docker_login(registry: str, username: str, password: str) -> bool:
    """Log in to a Docker registry.

    Returns True on success, False on failure.
    """
    try:
        client = get_docker_client()
        client.login(username=username, password=password, registry=registry)
        return True
    except Exception as e:
        print(f"Warning: Docker login failed: {e}", file=sys.stderr)
        return False


def crane_login(registry: str, username: str, password: str) -> bool:
    """Log in to a container registry using crane.

    This enables crane to push/pull from private registries.
    See: https://github.com/google/go-containerregistry/blob/main/cmd/crane/doc/crane_auth_login.md

    Returns True on success, False on failure.
    """
    crane = get_crane_path()

    cmd = [
        str(crane),
        "auth", "login",
        registry,
        "--username", username,
        "--password", password,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Warning: Crane login failed for {registry}: {result.stderr}", file=sys.stderr)
        return False

    return True


def login_to_all_registries() -> None:
    """Log in to all configured registries that have credentials.

    Logs in with both Docker and crane to enable:
    - Docker: Pulling base images during builds
    - Crane: Pushing images to registry
    """
    registries = get_registries()

    for reg in registries:
        auth = reg.get_auth()
        if auth:
            username, password = auth
            # Extract registry host (without path) for login
            registry_host = reg.url.split("/")[0]

            # Login with Docker (for pulling during builds)
            docker_login(registry_host, username, password)

            # Login with crane (for pushing)
            if crane_login(registry_host, username, password):
                print(f"Logged in to registry: {registry_host}")


# --- Garage (S3 cache) ---

def get_cache_endpoint_for_buildkit() -> str | None:
    """Get the S3 cache endpoint as seen by buildkitd.

    On macOS, buildkitd runs in a container and needs host.docker.internal.
    On Linux, buildkitd runs natively and can use localhost.

    Returns None if caching is disabled.
    """
    cache = get_cache_config()
    if not cache:
        return None

    endpoint = cache.endpoint

    # For local development (localhost), adjust for macOS container
    if platform.system().lower() == "darwin":
        if "localhost" in endpoint or "127.0.0.1" in endpoint:
            # Replace localhost with host.docker.internal for container access
            endpoint = endpoint.replace("localhost", "host.docker.internal")
            endpoint = endpoint.replace("127.0.0.1", "host.docker.internal")

    return endpoint


def check_cache_connection() -> bool:
    """Check if S3 cache endpoint is reachable."""
    cache = get_cache_config()
    if not cache:
        return False

    # Parse endpoint to extract host and port
    endpoint = cache.endpoint
    try:
        # Remove protocol
        if "://" in endpoint:
            endpoint = endpoint.split("://")[1]
        # Split host and port
        if ":" in endpoint:
            host, port_str = endpoint.split(":", 1)
            # Remove any path
            port_str = port_str.split("/")[0]
            port = int(port_str)
        else:
            host = endpoint.split("/")[0]
            port = 443 if cache.endpoint.startswith("https") else 80

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def get_crane_path() -> Path:
    """Get the path to the crane binary."""
    binary = get_bin_path() / "crane"
    if not binary.exists():
        raise RuntimeError(f"crane binary not found: {binary}")
    return binary


def get_native_platform() -> str:
    """Detect the native platform for the current system."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    elif machine in ("arm64", "aarch64"):
        return "linux/arm64"
    else:
        raise RuntimeError(f"Unsupported architecture: {machine}")


def normalize_platform(plat: str) -> str:
    """Normalize platform string to full form (e.g., 'amd64' -> 'linux/amd64')."""
    if plat not in PLATFORM_ALIASES:
        raise ValueError(f"Unknown platform: {plat}. Supported: {list(PLATFORM_ALIASES.keys())}")
    return PLATFORM_ALIASES[plat]


def platform_to_path(plat: str) -> str:
    """Convert platform to filesystem-safe path component (e.g., 'linux/amd64' -> 'linux-amd64')."""
    return plat.replace("/", "-")


def needs_emulation(target_platform: str) -> bool:
    """Check if building for target platform requires QEMU emulation."""
    native = get_native_platform()
    return target_platform != native


def is_binfmt_installed() -> bool:
    """Check if binfmt handlers are registered for cross-platform builds."""
    # Check if QEMU handlers are registered
    binfmt_misc = Path("/proc/sys/fs/binfmt_misc")
    if not binfmt_misc.exists():
        return False

    # Look for qemu handlers
    for entry in binfmt_misc.iterdir():
        if entry.name.startswith("qemu-"):
            return True

    return False


def ensure_binfmt() -> bool:
    """Ensure binfmt handlers are installed for cross-platform builds.

    Runs the binfmt setup container if needed (requires privileged).
    Returns True if emulation is available, False otherwise.
    """
    if is_binfmt_installed():
        return True

    print("Setting up QEMU emulation for cross-platform builds...")
    try:
        client = get_docker_client()
        result = client.containers.run(
            BINFMT_IMAGE,
            command=["--install", "all"],
            privileged=True,
            remove=True,
        )
        print("QEMU emulation configured successfully")
        return True
    except Exception as e:
        print(f"Warning: Failed to setup binfmt emulation: {e}", file=sys.stderr)
        print("Cross-platform builds may not work. Run manually:", file=sys.stderr)
        print(f"  docker run --privileged --rm {BINFMT_IMAGE} --install all", file=sys.stderr)
        return False


def push_to_registry(tar_path: Path, image_ref: str) -> bool:
    """Push a tar image to the local registry using crane.

    Args:
        tar_path: Path to the image tar file
        image_ref: Image reference (e.g., 'base:2025.09')

    Returns:
        True if successful, False otherwise
    """
    crane = get_crane_path()
    registry_ref = f"{get_registry_addr()}/{image_ref}"

    cmd = [
        str(crane),
        "push",
        str(tar_path),
        registry_ref,
    ]
    if is_registry_insecure():
        cmd.append("--insecure")

    print(f"Pushing to registry: {registry_ref}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Failed to push to registry: {result.stderr}", file=sys.stderr)
        return False

    return True


def get_aliases_for_tag(image_name: str, tag_name: str) -> list[str]:
    """Get all aliases that point to a specific tag.

    Scans dist/<image>/ for alias files (non-directories) where content matches tag_name.

    Args:
        image_name: Image name (e.g., 'dotnet')
        tag_name: Tag name to find aliases for (e.g., '9.0.300')

    Returns:
        List of alias names that point to this tag (e.g., ['9', '9.0'])
    """
    aliases = []
    dist_path = Path("dist") / image_name

    if not dist_path.exists():
        return aliases

    for entry in dist_path.iterdir():
        # Skip directories (actual tag builds) and special files
        if entry.is_dir() or entry.name.startswith(".") or entry.name == "index.html":
            continue

        # Read alias file content
        try:
            content = entry.read_text().strip()
            if content == tag_name:
                aliases.append(entry.name)
        except Exception:
            continue

    return aliases


def tag_aliases(image_ref: str, snapshot_id: str | None = None) -> int:
    """Apply all aliases for an image using crane tag.

    Tags an existing registry image with its aliases (e.g., dotnet:9.0.300 -> dotnet:9).

    Args:
        image_ref: Image reference in format 'name:tag' (e.g., 'dotnet:9.0.300')
        snapshot_id: Optional snapshot identifier for registry tags

    Returns:
        Exit code (0 for success, 1 if image not found or tagging failed)
    """
    if ":" not in image_ref:
        print(f"Error: Invalid image reference '{image_ref}', expected format: name:tag", file=sys.stderr)
        return 1

    name, tag = image_ref.split(":", 1)
    crane = get_crane_path()
    registry = get_registry_addr()

    # Build source image reference
    if snapshot_id:
        source_ref = f"{registry}/{name}:{tag}-{snapshot_id}"
    else:
        source_ref = f"{registry}/{name}:{tag}"

    # Check if source image exists
    check_cmd = [str(crane), "digest", source_ref]
    if is_registry_insecure():
        check_cmd.insert(2, "--insecure")
    result = subprocess.run(check_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: Image not found in registry: {source_ref}", file=sys.stderr)
        return 1

    # Get aliases for this tag
    aliases = get_aliases_for_tag(name, tag)
    if not aliases:
        print(f"No aliases found for {image_ref}")
        return 0

    print(f"Applying {len(aliases)} alias(es) for {image_ref}: {', '.join(aliases)}")

    # Tag each alias
    failed = []
    for alias in aliases:
        # Build the alias tag name (crane tag expects just the tag, not full ref)
        if snapshot_id:
            alias_tag = f"{alias}-{snapshot_id}"
        else:
            alias_tag = alias

        # crane tag IMG TAG - tags IMG with TAG
        tag_cmd = [str(crane), "tag", source_ref, alias_tag]
        if is_registry_insecure():
            tag_cmd.insert(2, "--insecure")
        tag_result = subprocess.run(tag_cmd, capture_output=True, text=True)

        if tag_result.returncode != 0:
            print(f"  Failed to tag {alias}: {tag_result.stderr}", file=sys.stderr)
            failed.append(alias)
        else:
            print(f"  Tagged: {registry}/{name}:{alias_tag}")

    if failed:
        print(f"Warning: Failed to apply {len(failed)} alias(es)", file=sys.stderr)
        return 1

    return 0


def check_image_exists(image_ref: str, snapshot_id: str | None = None) -> bool:
    """Check if an image exists in the registry.

    Args:
        image_ref: Image reference in format 'name:tag'
        snapshot_id: Optional snapshot identifier

    Returns:
        True if image exists, False otherwise
    """
    if ":" not in image_ref:
        return False

    name, tag = image_ref.split(":", 1)
    crane = get_crane_path()
    registry = get_registry_addr()

    if snapshot_id:
        full_ref = f"{registry}/{name}:{tag}-{snapshot_id}"
    else:
        full_ref = f"{registry}/{name}:{tag}"

    check_cmd = [str(crane), "digest", full_ref]
    if is_registry_insecure():
        check_cmd.insert(2, "--insecure")
    result = subprocess.run(check_cmd, capture_output=True, text=True)
    return result.returncode == 0


def get_local_images() -> set[str]:
    """Get set of image references available in dist/ (already built)."""
    images = set()
    dist_path = Path("dist")
    if not dist_path.exists():
        return images

    for image_dir in dist_path.iterdir():
        if not image_dir.is_dir():
            continue
        for tag_dir in image_dir.iterdir():
            if not tag_dir.is_dir():
                continue
            # Check for multi-platform (platform subdirs)
            has_platform = False
            for plat_dir in tag_dir.iterdir():
                if plat_dir.is_dir() and plat_dir.name.startswith("linux-"):
                    if (plat_dir / "image.tar").exists():
                        images.add(f"{image_dir.name}:{tag_dir.name}")
                        has_platform = True
                        break
            # Fallback to single image.tar
            if not has_platform and (tag_dir / "image.tar").exists():
                images.add(f"{image_dir.name}:{tag_dir.name}")

    return images


def rewrite_dockerfile_for_registry(dockerfile_path: Path, local_images: set[str], snapshot_id: str | None = None) -> str:
    """Rewrite Dockerfile FROM lines to use local registry for local base images.

    Args:
        dockerfile_path: Path to the original Dockerfile
        local_images: Set of image refs available in local registry (without snapshot suffix)
        snapshot_id: Optional snapshot ID - used to match snapshot-suffixed FROM refs

    Returns:
        Modified Dockerfile content
    """
    content = dockerfile_path.read_text()
    registry = get_registry_addr_for_buildkit()

    # Match FROM lines, capturing the image reference
    # Handles: FROM image:tag, FROM image:tag AS name, FROM image AS name
    from_pattern = re.compile(r'^(FROM\s+)([^\s]+)(.*)$', re.MULTILINE | re.IGNORECASE)

    def replace_from(match):
        prefix = match.group(1)
        image_ref = match.group(2)
        suffix = match.group(3)

        # Check if this image is one of our local images
        # Also check for snapshot-suffixed refs (e.g., base:2025.09-mr-123 → base:2025.09)
        base_ref = image_ref
        if snapshot_id and image_ref.endswith(f"-{snapshot_id}"):
            base_ref = image_ref[: -len(f"-{snapshot_id}")]

        if base_ref in local_images:
            # Use the full image_ref (which may include snapshot suffix from generate)
            return f"{prefix}{registry}/{image_ref}{suffix}"

        return match.group(0)

    return from_pattern.sub(replace_from, content)


def find_build_context(image_ref: str) -> Path:
    """Find the build context directory for an image reference.

    Args:
        image_ref: Image reference in format 'name:tag' (e.g., 'base:2025.9')

    Returns:
        Path to the build context in dist/
    """
    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}', expected format: name:tag")

    name, tag = image_ref.split(":", 1)
    context_path = Path("dist") / name / tag

    if not context_path.exists():
        raise FileNotFoundError(
            f"Build context not found: {context_path}\n"
            f"Run 'uv run image-manager' first to generate build contexts."
        )

    if not (context_path / "Dockerfile").exists():
        raise FileNotFoundError(
            f"Dockerfile not found in: {context_path}\n"
            f"Run 'uv run image-manager' first to generate Dockerfiles."
        )

    return context_path


def get_image_tar_path(image_ref: str) -> Path:
    """Get the path to the image tar file for an image reference."""
    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}', expected format: name:tag")

    name, tag = image_ref.split(":", 1)
    return Path("dist") / name / tag / "image.tar"


def get_platform_tar_path(image_ref: str, plat: str) -> Path:
    """Get the path to the platform-specific image tar file."""
    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}', expected format: name:tag")

    name, tag = image_ref.split(":", 1)
    platform_dir = platform_to_path(plat)
    return Path("dist") / name / tag / platform_dir / "image.tar"


def _get_git_revision() -> str | None:
    """Get the current git commit SHA if in a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _get_git_remote_url() -> str | None:
    """Get the git remote URL, converted to HTTPS format for source label."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Convert SSH URL to HTTPS if needed
            # git@github.com:user/repo.git -> https://github.com/user/repo
            if url.startswith("git@"):
                url = url.replace(":", "/").replace("git@", "https://")
            # Remove .git suffix
            if url.endswith(".git"):
                url = url[:-4]
            return url
    except Exception:
        pass
    return None


def _get_base_image_info(context_path: Path) -> tuple[str, str | None] | None:
    """Extract base image name and digest from Dockerfile and lock file.

    Args:
        context_path: Path to build context containing Dockerfile

    Returns:
        Tuple of (base_name, base_digest) or None if not found.
        base_digest may be None if not available in lock file.
    """
    dockerfile = context_path / "Dockerfile"
    if not dockerfile.exists():
        return None

    content = dockerfile.read_text()

    # Find the last FROM line (for multi-stage builds)
    from_pattern = re.compile(r"^FROM\s+([^\s]+)", re.MULTILINE | re.IGNORECASE)
    matches = from_pattern.findall(content)
    if not matches:
        return None

    base_ref = matches[-1]  # Last FROM is the effective base

    # Check if it's already a digest reference
    if "@sha256:" in base_ref:
        # Already pinned: ubuntu@sha256:abc123
        parts = base_ref.split("@")
        return (parts[0], parts[1])

    # Try to get digest from lock file
    lock_file = context_path.parent.parent.parent / "images"
    # Find the image.yml directory to locate packages.lock
    image_name = context_path.parent.parent.name
    for lock_path in Path("images").rglob("packages.lock"):
        if image_name in str(lock_path):
            try:
                import yaml
                data = yaml.safe_load(lock_path.read_text())
                if data and "bases" in data:
                    for base_name, base_info in data["bases"].items():
                        if base_name == base_ref or base_ref.startswith(base_name.split(":")[0]):
                            return (base_ref, base_info.get("digest"))
            except Exception:
                pass

    return (base_ref, None)


def _get_image_config(image_name: str):
    """Load image.yml config for an image to get description and licenses.

    Args:
        image_name: Image name (e.g., "base", "python")

    Returns:
        ImageConfig or None if not found
    """
    images_dir = Path("images")
    if not images_dir.exists():
        return None

    # Search for image.yml in the images directory
    for image_yml in images_dir.rglob("image.yml"):
        try:
            config = ConfigLoader.load(image_yml)
            # Check if this is the right image by name or directory
            if config.name == image_name:
                return config
            # Also check parent directory name
            if image_yml.parent.name == image_name or image_yml.parent.parent.name == image_name:
                return config
        except Exception:
            continue

    return None


def run_build_platform(
    image_ref: str,
    plat: str,
    context_path: Path | None = None,
    use_cache: bool = True,
    snapshot_id: str | None = None,
) -> int:
    """Build an image for a specific platform.

    Args:
        image_ref: Image reference in format 'name:tag'
        plat: Target platform (e.g., 'linux/amd64')
        context_path: Optional explicit path to build context
        use_cache: If True, use S3 cache via Garage
        snapshot_id: Optional snapshot identifier for registry tags

    Returns:
        Exit code (0 for success)
    """
    if context_path is None:
        context_path = find_build_context(image_ref)

    tar_path = get_platform_tar_path(image_ref, plat)
    tar_path.parent.mkdir(parents=True, exist_ok=True)

    buildctl = get_buildctl_path()
    addr = get_socket_addr()
    registry = get_registry_addr()
    platform_path = platform_to_path(plat)

    # Build cache arguments
    cache_args = []
    if use_cache:
        cache = get_cache_config()
        s3_endpoint = get_cache_endpoint_for_buildkit()
        if cache and s3_endpoint:
            cache_name = f"{image_ref.split(':')[0]}-{platform_path}"
            path_style = "true" if cache.use_path_style else "false"
            cache_args = [
                "--export-cache", f"type=s3,endpoint_url={s3_endpoint},bucket={cache.bucket},region={cache.region},name={cache_name},access_key_id={cache.access_key},secret_access_key={cache.secret_key},use_path_style={path_style},mode=max",
                "--import-cache", f"type=s3,endpoint_url={s3_endpoint},bucket={cache.bucket},region={cache.region},name={cache_name},access_key_id={cache.access_key},secret_access_key={cache.secret_key},use_path_style={path_style}",
            ]

    # Rewrite FROM for local base images
    dockerfile_path = context_path / "Dockerfile"
    local_images = get_local_images()
    modified_content = rewrite_dockerfile_for_registry(dockerfile_path, local_images, snapshot_id)
    original_content = dockerfile_path.read_text()

    # Platform-specific image name for registry
    platform_image_ref = f"{image_ref}-{platform_path}"

    # Extract name and tag for labels
    image_name, image_tag = image_ref.split(":", 1)

    # Build reproducibility args
    repro_args = [
        "--opt", f"build-arg:SOURCE_DATE_EPOCH={SOURCE_DATE_EPOCH}",
    ]
    policy_file = context_path / "policy.json"
    if policy_file.exists():
        repro_args.extend(["--source-policy-file", str(policy_file)])

    # OCI image labels (https://github.com/opencontainers/image-spec/blob/main/annotations.md)
    label_args = [
        "--opt", f"label:org.opencontainers.image.ref.name={image_name}",
        "--opt", f"label:org.opencontainers.image.version={image_tag}",
        "--opt", f"label:org.opencontainers.image.title={image_name}",
        "--opt", f"label:org.opencontainers.image.created={datetime.now(tz=timezone.utc).isoformat()}",
    ]

    # Add git revision if available
    git_rev = _get_git_revision()
    if git_rev:
        label_args.extend(["--opt", f"label:org.opencontainers.image.revision={git_rev}"])

    # Add source from git remote
    git_source = _get_git_remote_url()
    if git_source:
        label_args.extend(["--opt", f"label:org.opencontainers.image.source={git_source}"])

    # Add global labels from config
    labels_config = get_labels_config()
    if labels_config.vendor:
        label_args.extend(["--opt", f"label:org.opencontainers.image.vendor={labels_config.vendor}"])
    if labels_config.authors:
        label_args.extend(["--opt", f"label:org.opencontainers.image.authors={labels_config.authors}"])
    if labels_config.url:
        # Apply %image% and %tag% placeholders
        url = labels_config.url.replace("%image%", image_name).replace("%tag%", image_tag)
        label_args.extend(["--opt", f"label:org.opencontainers.image.url={url}"])
    if labels_config.documentation:
        # Apply %image% and %tag% placeholders
        doc_url = labels_config.documentation.replace("%image%", image_name).replace("%tag%", image_tag)
        label_args.extend(["--opt", f"label:org.opencontainers.image.documentation={doc_url}"])

    # Add per-image labels from image.yml (description, licenses)
    image_config = _get_image_config(image_name)
    if image_config:
        if image_config.description:
            label_args.extend(["--opt", f"label:org.opencontainers.image.description={image_config.description}"])
        # Image-level licenses override global
        if image_config.licenses:
            label_args.extend(["--opt", f"label:org.opencontainers.image.licenses={image_config.licenses}"])
        elif labels_config.licenses:
            label_args.extend(["--opt", f"label:org.opencontainers.image.licenses={labels_config.licenses}"])
    elif labels_config.licenses:
        label_args.extend(["--opt", f"label:org.opencontainers.image.licenses={labels_config.licenses}"])

    # Add base image labels from Dockerfile
    base_info = _get_base_image_info(context_path)
    if base_info:
        base_name, base_digest = base_info
        label_args.extend(["--opt", f"label:org.opencontainers.image.base.name={base_name}"])
        if base_digest:
            label_args.extend(["--opt", f"label:org.opencontainers.image.base.digest={base_digest}"])

    if modified_content != original_content:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_dockerfile = Path(tmpdir) / "Dockerfile"
            tmp_dockerfile.write_text(modified_content)

            cmd = [
                str(buildctl), "--addr", addr, "build",
                "--frontend", "dockerfile.v0",
                "--local", f"context={context_path}",
                "--local", f"dockerfile={tmpdir}",
                "--output", f"type=docker,name={platform_image_ref},dest={tar_path},rewrite-timestamp=true",
                "--opt", f"platform={plat}",
            ] + repro_args + label_args + cache_args

            print(f"Building {image_ref} for {plat}...")
            result = subprocess.run(cmd)
    else:
        cmd = [
            str(buildctl), "--addr", addr, "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={context_path}",
            "--local", f"dockerfile={context_path}",
            "--output", f"type=docker,name={platform_image_ref},dest={tar_path},rewrite-timestamp=true",
            "--opt", f"platform={plat}",
        ] + repro_args + label_args + cache_args

        print(f"Building {image_ref} for {plat}...")
        result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"Platform image saved to: {tar_path}")

        # Push platform-specific image to registry
        registry_ref = platform_image_ref
        if snapshot_id:
            registry_ref = f"{image_ref}-{snapshot_id}-{platform_path}"

        if push_to_registry(tar_path, registry_ref):
            print(f"Platform image pushed: {get_registry_addr()}/{registry_ref}")

    return result.returncode


def create_multiplatform_manifest(
    image_ref: str,
    platforms: list[str],
    snapshot_id: str | None = None,
) -> int:
    """Create a multi-platform manifest from platform-specific images.

    Uses crane to create an OCI index combining all platform images.

    Args:
        image_ref: Base image reference (e.g., 'base:2025.09')
        platforms: List of platforms that were built
        snapshot_id: Optional snapshot identifier

    Returns:
        Exit code (0 for success)
    """
    crane = get_crane_path()
    registry = get_registry_addr()

    # Output path for multi-platform manifest
    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}'")

    name, tag = image_ref.split(":", 1)
    manifest_tar = Path("dist") / name / tag / "image.tar"

    # Build list of platform image references in registry
    platform_refs = []
    for plat in platforms:
        platform_path = platform_to_path(plat)
        if snapshot_id:
            ref = f"{registry}/{image_ref}-{snapshot_id}-{platform_path}"
        else:
            ref = f"{registry}/{image_ref}-{platform_path}"
        platform_refs.append(ref)

    # Create manifest using crane index append
    manifest_ref = f"{registry}/{image_ref}"
    if snapshot_id:
        manifest_ref = f"{registry}/{image_ref}-{snapshot_id}"

    print(f"Creating multi-platform manifest: {manifest_ref}")

    # Use crane to create index
    cmd = [
        str(crane), "index", "append",
        "-t", manifest_ref,
    ]
    if is_registry_insecure():
        cmd.insert(3, "--insecure")
    for ref in platform_refs:
        cmd.extend(["-m", ref])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Failed to create manifest: {result.stderr}", file=sys.stderr)
        return result.returncode

    print(f"Multi-platform manifest pushed: {manifest_ref}")

    # Export manifest to tar
    export_cmd = [
        str(crane), "pull",
        manifest_ref, str(manifest_tar),
    ]
    if is_registry_insecure():
        export_cmd.insert(2, "--insecure")

    export_result = subprocess.run(export_cmd, capture_output=True, text=True)
    if export_result.returncode == 0:
        print(f"Multi-platform image saved to: {manifest_tar}")
    else:
        print(f"Warning: Could not export manifest to tar: {export_result.stderr}", file=sys.stderr)

    return 0


def create_manifest_from_registry(
    image_ref: str,
    snapshot_id: str | None = None,
) -> int:
    """Create a multi-platform manifest from platform images already in registry.

    Checks which platform images exist in the registry and combines them into
    a multi-platform manifest. Useful for combining images built on separate
    native runners.

    Args:
        image_ref: Image reference (e.g., 'base:2025.09')
        snapshot_id: Optional snapshot identifier

    Returns:
        Exit code (0 for success)
    """
    if not check_registry_connection():
        print(f"Error: Registry not reachable at {get_registry_url()}", file=sys.stderr)
        print("Run 'docker compose up -d' to start infrastructure services.", file=sys.stderr)
        return 1

    crane = get_crane_path()
    registry = get_registry_addr()

    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}'")

    name, tag = image_ref.split(":", 1)

    # Check which platform images exist in registry
    available_platforms = []
    for plat in SUPPORTED_PLATFORMS:
        platform_path = platform_to_path(plat)
        if snapshot_id:
            ref = f"{registry}/{image_ref}-{snapshot_id}-{platform_path}"
        else:
            ref = f"{registry}/{image_ref}-{platform_path}"

        # Check if image exists using crane digest
        check_cmd = [str(crane), "digest", ref]
        if is_registry_insecure():
            check_cmd.insert(2, "--insecure")
        result = subprocess.run(check_cmd, capture_output=True, text=True)

        if result.returncode == 0:
            print(f"Found platform image: {ref}")
            available_platforms.append(plat)
        else:
            print(f"Platform image not found: {ref}")

    if not available_platforms:
        print("Error: No platform images found in registry", file=sys.stderr)
        return 1

    if len(available_platforms) < 2:
        print(f"Warning: Only {len(available_platforms)} platform(s) found, need 2+ for multi-platform manifest")
        print("Proceeding with single platform...")

    # Create the manifest
    return create_multiplatform_manifest(image_ref, available_platforms, snapshot_id)


def run_build(
    image_ref: str,
    context_path: Path | None = None,
    use_cache: bool = True,
    snapshot_id: str | None = None,
    platforms: list[str] | None = None,
) -> int:
    """Run buildctl to build an image for one or more platforms.

    By default, builds for all supported platforms and creates a multi-platform
    manifest. Use platforms parameter to limit to specific platform(s).

    Args:
        image_ref: Image reference in format 'name:tag'
        context_path: Optional explicit path to build context
        use_cache: If True, use S3 cache via Garage
        snapshot_id: Optional snapshot identifier for registry tags
        platforms: List of platforms to build. None = all platforms.

    Returns:
        Exit code from buildctl
    """
    # Check registry connection (to the push registry)
    push_registry = get_push_registry()
    if not check_registry_connection():
        print(f"Error: Registry not reachable at {push_registry.url}", file=sys.stderr)
        print("Run 'docker compose up -d' to start infrastructure services.", file=sys.stderr)
        return 1

    # Log in to all configured registries (for pulling and pushing)
    login_to_all_registries()

    if use_cache and not check_cache_connection():
        print("Warning: S3 cache not reachable, building without cache", file=sys.stderr)
        use_cache = False

    # Normalize platforms
    if platforms is None:
        platforms = SUPPORTED_PLATFORMS.copy()
    else:
        platforms = [normalize_platform(p) for p in platforms]

    # Check if we need emulation
    native = get_native_platform()
    needs_cross = any(p != native for p in platforms)

    # Setup emulation if needed
    if needs_cross:
        if not ensure_binfmt():
            print(f"Warning: Emulation setup failed, limiting to native platform ({native})", file=sys.stderr)
            platforms = [native]

    if context_path is None:
        context_path = find_build_context(image_ref)

    print(f"Building {image_ref} for platforms: {', '.join(platforms)}")

    # Build each platform
    successful_platforms = []
    for plat in platforms:
        result = run_build_platform(
            image_ref=image_ref,
            plat=plat,
            context_path=context_path,
            use_cache=use_cache,
            snapshot_id=snapshot_id,
        )
        if result == 0:
            successful_platforms.append(plat)
        else:
            print(f"Failed to build for {plat}", file=sys.stderr)

    if not successful_platforms:
        print("Error: All platform builds failed", file=sys.stderr)
        return 1

    # Create multi-platform manifest if multiple platforms built
    if len(successful_platforms) > 1:
        manifest_result = create_multiplatform_manifest(
            image_ref=image_ref,
            platforms=successful_platforms,
            snapshot_id=snapshot_id,
        )
        if manifest_result != 0:
            print("Warning: Failed to create multi-platform manifest", file=sys.stderr)
    elif len(successful_platforms) == 1:
        # Single platform: copy to main image.tar location for compatibility
        plat = successful_platforms[0]
        platform_tar = get_platform_tar_path(image_ref, plat)
        main_tar = get_image_tar_path(image_ref)

        if platform_tar.exists():
            main_tar.parent.mkdir(parents=True, exist_ok=True)
            if main_tar.exists() or main_tar.is_symlink():
                main_tar.unlink()
            shutil.copy2(platform_tar, main_tar)
            print(f"Image saved to: {main_tar}")

    failed_count = len(platforms) - len(successful_platforms)
    if failed_count > 0:
        print(f"Warning: {failed_count} platform(s) failed to build", file=sys.stderr)
        return 1 if not successful_platforms else 0

    # Generate tag report
    name, tag = image_ref.split(":", 1)
    report_path = generate_tag_report(name, tag, snapshot_id)
    print(f"Tag report: {report_path}")

    # Apply aliases to the built image
    alias_result = tag_aliases(image_ref, snapshot_id)
    if alias_result != 0:
        print("Warning: Some aliases failed to apply", file=sys.stderr)

    return 0


def main() -> None:
    """CLI entrypoint for build-image command."""
    from manager.cli import CLI

    cli = (
        CLI(
            name="build-image",
            description="Build container images using buildkit.\nOutput: dist/<name>/<tag>/image.tar",
            daemon_name="buildkitd",
            daemon_addr_fn=get_socket_addr,
            is_running_fn=is_buildkitd_running,
            start_fn=start_buildkitd,
            stop_fn=stop_buildkitd,
        )
        .add_option("context", "Build context path (default: dist/<name>/<tag>)")
        .add_example("start")
        .add_example("base:2025.9")
        .add_example("dotnet:9.0.300 --context ./custom")
        .add_example("stop")
    )

    image_ref, opts = cli.parse_args()

    context_path = Path(opts["context"]) if "context" in opts else None

    try:
        exit_code = run_build(image_ref, context_path)
        sys.exit(exit_code)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
