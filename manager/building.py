"""Wrapper for buildkit (buildctl/buildkitd) binaries."""

import os
import platform
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import docker
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

# Garage for S3-compatible build cache
GARAGE_CONTAINER_NAME = "image-manager-garage"
GARAGE_S3_PORT = 3900
GARAGE_RPC_PORT = 3901
GARAGE_ADMIN_PORT = 3903
GARAGE_IMAGE = "dxflrs/garage:v2.1.0"
GARAGE_BUCKET = "buildkit-cache"
GARAGE_REGION = "garage"
# These will be generated on first start
GARAGE_ACCESS_KEY_ID = "GK" + "buildkit" + "0" * 24  # Placeholder, actual from garage
GARAGE_SECRET_KEY = "buildkitsecret" + "0" * 18  # Placeholder, actual from garage

# Platform support
SUPPORTED_PLATFORMS = ["linux/amd64", "linux/arm64"]
PLATFORM_ALIASES = {
    "amd64": "linux/amd64",
    "arm64": "linux/arm64",
    "linux/amd64": "linux/amd64",
    "linux/arm64": "linux/arm64",
}
BINFMT_IMAGE = "tonistiigi/binfmt"


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
    """Start buildkitd natively (Linux only)."""
    if is_buildkitd_running():
        print(f"buildkitd is already running (socket: {DEFAULT_SOCKET_PATH})")
        return 0

    DEFAULT_BUILDKIT_DIR.mkdir(parents=True, exist_ok=True)

    buildkitd = get_buildkitd_path()

    cmd = [
        str(buildkitd),
        "--addr", get_socket_addr(),
        "--root", str(DEFAULT_BUILDKIT_DIR / "root"),
    ]

    print(f"Starting buildkitd: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    get_pid_file().write_text(str(proc.pid))

    for _ in range(30):
        if DEFAULT_SOCKET_PATH.exists():
            print(f"buildkitd started (pid: {proc.pid}, socket: {DEFAULT_SOCKET_PATH})")
            return 0
        time.sleep(0.1)

    print("Warning: buildkitd started but socket not yet available", file=sys.stderr)
    return 0


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
    """Ensure buildkitd is running, start if needed."""
    if is_buildkitd_running():
        return True
    return start_buildkitd() == 0


# --- Registry management ---

def get_registry_addr() -> str:
    """Get the local registry address for external access (host)."""
    return f"localhost:{REGISTRY_PORT}"


def get_registry_addr_for_buildkit() -> str:
    """Get the local registry address as seen by buildkitd.

    On macOS, buildkitd runs in a container and needs host.docker.internal.
    On Linux, buildkitd runs natively and can use localhost.
    """
    if platform.system().lower() == "darwin":
        return f"host.docker.internal:{REGISTRY_PORT}"
    return f"localhost:{REGISTRY_PORT}"


def is_registry_running() -> bool:
    """Check if the registry container is running."""
    try:
        client = get_docker_client()
        container = client.containers.get(REGISTRY_CONTAINER_NAME)
        return container.status == "running"
    except NotFound:
        return False
    except Exception:
        return False


def start_registry() -> int:
    """Start local registry container."""
    if is_registry_running():
        print("registry container is already running")
        return 0

    client = get_docker_client()

    # Remove any existing container
    try:
        old = client.containers.get(REGISTRY_CONTAINER_NAME)
        old.remove(force=True)
    except NotFound:
        pass

    print(f"Starting registry container ({REGISTRY_IMAGE})...")
    try:
        # Registry always listens on 5000 internally, map to external port
        client.containers.run(
            REGISTRY_IMAGE,
            name=REGISTRY_CONTAINER_NAME,
            detach=True,
            ports={"5000/tcp": ("127.0.0.1", REGISTRY_PORT)},
        )
    except APIError as e:
        print(f"Failed to start registry container: {e}", file=sys.stderr)
        return 1

    # Wait for registry to be ready
    print("Waiting for registry to be ready...")
    for _ in range(50):
        if is_port_open(REGISTRY_PORT):
            print(f"registry container started (addr: {get_registry_addr()})")
            return 0
        time.sleep(0.1)

    print("Warning: registry started but port not yet available", file=sys.stderr)
    return 0


def stop_registry() -> int:
    """Stop the registry container."""
    try:
        client = get_docker_client()
        container = client.containers.get(REGISTRY_CONTAINER_NAME)
        container.remove(force=True)
        print("Stopped registry container")
    except NotFound:
        print("registry container was not running")
    except Exception as e:
        print(f"Error stopping registry: {e}", file=sys.stderr)
        return 1
    return 0


def ensure_registry() -> bool:
    """Ensure registry is running, start if needed."""
    if is_registry_running():
        return True
    return start_registry() == 0


# --- Garage (S3 cache) management ---

def get_garage_config_dir() -> Path:
    """Get the directory for Garage configuration and data."""
    return DEFAULT_BUILDKIT_DIR / "garage"


def get_garage_credentials_file() -> Path:
    """Get the path to stored Garage credentials."""
    return get_garage_config_dir() / "credentials.json"


def get_garage_s3_endpoint() -> str:
    """Get the Garage S3 endpoint URL."""
    return f"http://localhost:{GARAGE_S3_PORT}"


def get_garage_s3_endpoint_for_buildkit() -> str:
    """Get the Garage S3 endpoint as seen by buildkitd.

    On macOS, buildkitd runs in a container and needs host.docker.internal.
    On Linux, buildkitd runs natively and can use localhost.
    """
    if platform.system().lower() == "darwin":
        return f"http://host.docker.internal:{GARAGE_S3_PORT}"
    return f"http://localhost:{GARAGE_S3_PORT}"


def is_garage_running() -> bool:
    """Check if the Garage container is running."""
    try:
        client = get_docker_client()
        container = client.containers.get(GARAGE_CONTAINER_NAME)
        return container.status == "running"
    except NotFound:
        return False
    except Exception:
        return False


def get_garage_credentials() -> tuple[str, str] | None:
    """Get stored Garage credentials (access_key_id, secret_key)."""
    creds_file = get_garage_credentials_file()
    if not creds_file.exists():
        return None
    try:
        import json
        creds = json.loads(creds_file.read_text())
        return creds.get("access_key_id"), creds.get("secret_key")
    except Exception:
        return None


def save_garage_credentials(access_key_id: str, secret_key: str) -> None:
    """Save Garage credentials to file."""
    import json
    creds_file = get_garage_credentials_file()
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    creds_file.write_text(json.dumps({
        "access_key_id": access_key_id,
        "secret_key": secret_key,
    }))


def generate_garage_config() -> str:
    """Generate a minimal garage.toml for single-node setup."""
    import secrets
    rpc_secret = secrets.token_hex(32)
    admin_token = secrets.token_hex(32)

    return f"""
replication_factor = 1
consistency_mode = "consistent"

metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"

db_engine = "lmdb"

rpc_secret = "{rpc_secret}"
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"

[s3_api]
api_bind_addr = "[::]:3900"
s3_region = "{GARAGE_REGION}"
root_domain = ".s3.garage"

[admin]
api_bind_addr = "0.0.0.0:3903"
admin_token = "{admin_token}"
"""


def start_garage() -> int:
    """Start Garage container for S3-compatible cache."""
    if is_garage_running():
        print("garage container is already running")
        return 0

    client = get_docker_client()
    config_dir = get_garage_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    # Remove any existing container
    try:
        old = client.containers.get(GARAGE_CONTAINER_NAME)
        old.remove(force=True)
    except NotFound:
        pass

    # Generate config if not exists
    config_file = config_dir / "garage.toml"
    if not config_file.exists():
        config_file.write_text(generate_garage_config())

    # Create data directories
    (config_dir / "meta").mkdir(exist_ok=True)
    (config_dir / "data").mkdir(exist_ok=True)

    print(f"Starting garage container ({GARAGE_IMAGE})...")
    try:
        container = client.containers.run(
            GARAGE_IMAGE,
            name=GARAGE_CONTAINER_NAME,
            detach=True,
            ports={
                f"{GARAGE_S3_PORT}/tcp": ("127.0.0.1", GARAGE_S3_PORT),
                f"{GARAGE_RPC_PORT}/tcp": ("127.0.0.1", GARAGE_RPC_PORT),
                f"{GARAGE_ADMIN_PORT}/tcp": ("127.0.0.1", GARAGE_ADMIN_PORT),
            },
            volumes={
                str(config_file.absolute()): {"bind": "/etc/garage.toml", "mode": "ro"},
                str((config_dir / "meta").absolute()): {"bind": "/var/lib/garage/meta", "mode": "rw"},
                str((config_dir / "data").absolute()): {"bind": "/var/lib/garage/data", "mode": "rw"},
            },
        )
    except APIError as e:
        print(f"Failed to start garage container: {e}", file=sys.stderr)
        return 1

    # Wait for garage to be ready
    print("Waiting for garage to be ready...")
    for _ in range(100):  # 10 second timeout
        if is_port_open(GARAGE_S3_PORT):
            break
        time.sleep(0.1)
    else:
        print("Warning: garage started but S3 port not yet available", file=sys.stderr)

    # Initialize the cluster if needed
    time.sleep(1)  # Give garage a moment to fully start
    if not _initialize_garage_cluster(container):
        print("Warning: Failed to initialize garage cluster", file=sys.stderr)
        return 1

    print(f"garage container started (S3 endpoint: {get_garage_s3_endpoint()})")
    return 0


def _initialize_garage_cluster(container) -> bool:
    """Initialize Garage cluster: configure node, create bucket, create key."""
    import json

    try:
        # Get node ID - retry a few times as garage might need a moment
        for attempt in range(5):
            exit_code, output = container.exec_run("/garage status")
            output_str = output.decode() if isinstance(output, bytes) else output
            if exit_code == 0 and "HEALTHY NODES" in output_str:
                break
            time.sleep(1)
        else:
            print("Garage status check failed after retries", file=sys.stderr)
            return False

        # Check if already configured (look for NO ROLE ASSIGNED)
        if "NO ROLE ASSIGNED" in output_str:
            # Extract node ID from status table output
            # Format: "ID                Hostname      Address..."
            #         "6278c0a8e88e98d7  hostname      127.0.0.1:3901..."
            lines = output_str.split("\n")
            node_id = None
            in_nodes_section = False
            for line in lines:
                if "HEALTHY NODES" in line:
                    in_nodes_section = True
                    continue
                if in_nodes_section and line.strip() and not line.startswith("ID "):
                    # First column is the node ID (16 hex chars)
                    parts = line.split()
                    if parts and len(parts[0]) == 16:
                        node_id = parts[0]
                        break

            if not node_id:
                print("Could not find node ID in garage status", file=sys.stderr)
                print(f"Output was: {output_str}", file=sys.stderr)
                return False

            # Configure node with capacity
            print(f"Configuring garage node {node_id}...")
            exit_code, output = container.exec_run(
                f"/garage layout assign -z dc1 -c 1G {node_id}"
            )
            if exit_code != 0:
                print(f"Failed to assign layout: {output}", file=sys.stderr)
                return False

            # Apply layout
            exit_code, output = container.exec_run("/garage layout apply --version 1")
            if exit_code != 0:
                # Try without version if layout already exists
                exit_code, output = container.exec_run("/garage layout apply")
                if exit_code != 0:
                    print(f"Failed to apply layout: {output}", file=sys.stderr)
                    return False

            # Wait for layout to be applied
            time.sleep(1)

        # Check if bucket exists - retry as layout might still be initializing
        for attempt in range(5):
            exit_code, output = container.exec_run("/garage bucket list")
            output_str = output.decode() if isinstance(output, bytes) else output
            if exit_code == 0:
                break
            time.sleep(1)
        else:
            print(f"Failed to list buckets: {output_str}", file=sys.stderr)
            return False

        if GARAGE_BUCKET not in output_str:
            print(f"Creating bucket '{GARAGE_BUCKET}'...")
            exit_code, output = container.exec_run(f"/garage bucket create {GARAGE_BUCKET}")
            if exit_code != 0:
                print(f"Failed to create bucket: {output}", file=sys.stderr)
                return False

        # Check if we have stored credentials
        creds = get_garage_credentials()
        if creds is None:
            # Create access key
            print("Creating S3 access key...")
            exit_code, output = container.exec_run("/garage key create buildkit-key")
            output_str = output.decode() if isinstance(output, bytes) else output

            # Parse key info - format varies, look for Key ID and Secret key
            access_key = None
            secret_key = None
            for line in output_str.split("\n"):
                if "Key ID:" in line:
                    access_key = line.split("Key ID:")[1].strip()
                elif "Secret key:" in line:
                    secret_key = line.split("Secret key:")[1].strip()

            if not access_key or not secret_key:
                # Try alternative format
                exit_code, output = container.exec_run("/garage key info buildkit-key")
                output_str = output.decode() if isinstance(output, bytes) else output
                for line in output_str.split("\n"):
                    if "Key ID:" in line:
                        access_key = line.split("Key ID:")[1].strip()
                    elif "Secret key:" in line:
                        secret_key = line.split("Secret key:")[1].strip()

            if access_key and secret_key:
                save_garage_credentials(access_key, secret_key)
                print(f"Created and saved access key: {access_key[:10]}...")
            else:
                print("Warning: Could not parse access key from garage output", file=sys.stderr)
                return False

            # Grant bucket permissions
            container.exec_run(f"/garage bucket allow --read --write --owner {GARAGE_BUCKET} --key buildkit-key")

        return True

    except Exception as e:
        print(f"Error initializing garage: {e}", file=sys.stderr)
        return False


def stop_garage() -> int:
    """Stop the Garage container."""
    try:
        client = get_docker_client()
        container = client.containers.get(GARAGE_CONTAINER_NAME)
        container.remove(force=True)
        print("Stopped garage container")
    except NotFound:
        print("garage container was not running")
    except Exception as e:
        print(f"Error stopping garage: {e}", file=sys.stderr)
        return 1
    return 0


def ensure_garage() -> bool:
    """Ensure Garage is running, start if needed."""
    if is_garage_running():
        return True
    return start_garage() == 0


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
        "--insecure",  # Allow HTTP for localhost
    ]

    print(f"Pushing to registry: {registry_ref}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Failed to push to registry: {result.stderr}", file=sys.stderr)
        return False

    return True


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
            if (tag_dir / "image.tar").exists():
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
        creds = get_garage_credentials()
        if creds:
            access_key, secret_key = creds
            s3_endpoint = get_garage_s3_endpoint_for_buildkit()
            cache_name = f"{image_ref.split(':')[0]}-{platform_path}"
            cache_args = [
                "--export-cache", f"type=s3,endpoint_url={s3_endpoint},bucket={GARAGE_BUCKET},region={GARAGE_REGION},name={cache_name},access_key_id={access_key},secret_access_key={secret_key},use_path_style=true,mode=max",
                "--import-cache", f"type=s3,endpoint_url={s3_endpoint},bucket={GARAGE_BUCKET},region={GARAGE_REGION},name={cache_name},access_key_id={access_key},secret_access_key={secret_key},use_path_style=true",
            ]

    # Rewrite FROM for local base images
    dockerfile_path = context_path / "Dockerfile"
    local_images = get_local_images()
    modified_content = rewrite_dockerfile_for_registry(dockerfile_path, local_images, snapshot_id)
    original_content = dockerfile_path.read_text()

    # Platform-specific image name for registry
    platform_image_ref = f"{image_ref}-{platform_path}"

    if modified_content != original_content:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_dockerfile = Path(tmpdir) / "Dockerfile"
            tmp_dockerfile.write_text(modified_content)

            cmd = [
                str(buildctl), "--addr", addr, "build",
                "--frontend", "dockerfile.v0",
                "--local", f"context={context_path}",
                "--local", f"dockerfile={tmpdir}",
                "--output", f"type=docker,name={platform_image_ref},dest={tar_path}",
                "--opt", f"platform={plat}",
            ] + cache_args

            print(f"Building {image_ref} for {plat}...")
            result = subprocess.run(cmd)
    else:
        cmd = [
            str(buildctl), "--addr", addr, "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={context_path}",
            "--local", f"dockerfile={context_path}",
            "--output", f"type=docker,name={platform_image_ref},dest={tar_path}",
            "--opt", f"platform={plat}",
        ] + cache_args

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
        "--insecure",
        "-t", manifest_ref,
    ]
    for ref in platform_refs:
        cmd.extend(["-m", ref])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Failed to create manifest: {result.stderr}", file=sys.stderr)
        return result.returncode

    print(f"Multi-platform manifest pushed: {manifest_ref}")

    # Export manifest to tar
    export_cmd = [
        str(crane), "pull", "--insecure",
        manifest_ref, str(manifest_tar),
    ]

    export_result = subprocess.run(export_cmd, capture_output=True, text=True)
    if export_result.returncode == 0:
        print(f"Multi-platform image saved to: {manifest_tar}")
    else:
        print(f"Warning: Could not export manifest to tar: {export_result.stderr}", file=sys.stderr)

    return 0


def run_build(
    image_ref: str,
    context_path: Path | None = None,
    auto_start: bool = True,
    use_cache: bool = True,
    snapshot_id: str | None = None,
) -> int:
    """Run buildctl to build an image.

    Outputs the image as a tar archive in dist/<name>/<tag>/image.tar.
    Also pushes to local registry for dependent builds.

    Args:
        image_ref: Image reference in format 'name:tag'
        context_path: Optional explicit path to build context. If not provided,
                      will be derived from dist/<name>/<tag>/
        auto_start: If True, automatically start buildkitd and registry if not running
        use_cache: If True, use S3 cache via Garage for faster builds
        snapshot_id: Optional snapshot identifier (e.g., CI run ID) to append to
                     registry tags. Creates additional tag like 'base:2025.09-run-12345'

    Returns:
        Exit code from buildctl
    """
    if auto_start:
        if not ensure_buildkitd():
            print("Error: Failed to start buildkitd", file=sys.stderr)
            return 1
        if not ensure_registry():
            print("Error: Failed to start registry", file=sys.stderr)
            return 1
        if use_cache and not ensure_garage():
            print("Warning: Failed to start garage, building without cache", file=sys.stderr)
            use_cache = False

    if context_path is None:
        context_path = find_build_context(image_ref)

    tar_path = get_image_tar_path(image_ref)

    buildctl = get_buildctl_path()
    addr = get_socket_addr()
    registry = get_registry_addr()

    # Build cache arguments if garage is available
    cache_args = []
    if use_cache:
        creds = get_garage_credentials()
        if creds:
            access_key, secret_key = creds
            s3_endpoint = get_garage_s3_endpoint_for_buildkit()
            # Cache name based on image name (without tag) for better cache sharing
            cache_name = image_ref.split(":")[0]
            cache_args = [
                "--export-cache", f"type=s3,endpoint_url={s3_endpoint},bucket={GARAGE_BUCKET},region={GARAGE_REGION},name={cache_name},access_key_id={access_key},secret_access_key={secret_key},use_path_style=true,mode=max",
                "--import-cache", f"type=s3,endpoint_url={s3_endpoint},bucket={GARAGE_BUCKET},region={GARAGE_REGION},name={cache_name},access_key_id={access_key},secret_access_key={secret_key},use_path_style=true",
            ]
            print(f"Using S3 cache: {s3_endpoint}/{GARAGE_BUCKET}/{cache_name}")
        else:
            print("Warning: No garage credentials found, building without cache", file=sys.stderr)

    # Check if we need to rewrite FROM for local base images
    dockerfile_path = context_path / "Dockerfile"
    local_images = get_local_images()

    # Use temp dir for modified Dockerfile if needed
    modified_content = rewrite_dockerfile_for_registry(dockerfile_path, local_images, snapshot_id)
    original_content = dockerfile_path.read_text()

    if modified_content != original_content:
        # Create temp directory with modified Dockerfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_dockerfile = Path(tmpdir) / "Dockerfile"
            tmp_dockerfile.write_text(modified_content)
            print(f"Rewriting FROM to use registry for local base images")

            cmd = [
                str(buildctl),
                "--addr", addr,
                "build",
                "--frontend", "dockerfile.v0",
                "--local", f"context={context_path}",
                "--local", f"dockerfile={tmpdir}",
                "--output", f"type=docker,name={image_ref},dest={tar_path}",
                "--opt", f"build-arg:BUILDKIT_INSECURE_REGISTRY={registry}",
            ] + cache_args

            print(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd)
    else:
        cmd = [
            str(buildctl),
            "--addr", addr,
            "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={context_path}",
            "--local", f"dockerfile={context_path}",
            "--output", f"type=docker,name={image_ref},dest={tar_path}",
        ] + cache_args

        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"Image saved to: {tar_path}")

        # Determine registry tag: snapshot (MR/branch) or standard (main)
        if snapshot_id:
            registry_ref = f"{image_ref}-{snapshot_id}"
        else:
            registry_ref = image_ref

        if push_to_registry(tar_path, registry_ref):
            print(f"Image pushed to registry: {registry}/{registry_ref}")
        else:
            print("Warning: Failed to push to registry", file=sys.stderr)

    return result.returncode


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
