"""Package locking for reproducible builds."""

import platform
import re
import subprocess
import urllib.request
import urllib.parse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml


def get_syft_path() -> Path:
    """Get the path to the syft binary."""
    system = platform.system().lower()
    arch = platform.machine().lower()

    if system == "darwin" and arch == "arm64":
        plat = "darwin-arm64"
    elif system == "linux" and arch in ("x86_64", "amd64"):
        plat = "linux-amd64"
    elif system == "linux" and arch in ("arm64", "aarch64"):
        plat = "linux-arm64"
    else:
        plat = f"{system}-{arch}"

    return Path(__file__).parent.parent / "bin" / plat / "syft"


def extract_distro_from_image(image_tar: Path) -> dict | None:
    """Extract distro information from an image tar using syft.

    Args:
        image_tar: Path to the image tar file

    Returns:
        Dict with distro info (id, versionID, versionCodename) or None
    """
    syft = get_syft_path()
    if not syft.exists():
        return None

    if not image_tar.exists():
        return None

    try:
        result = subprocess.run(
            [str(syft), "scan", f"docker-archive:{image_tar}", "-o", "json", "-q"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            distro = data.get("distro", {})
            if distro:
                return {
                    "id": distro.get("id"),
                    "versionID": distro.get("versionID"),
                    "versionCodename": distro.get("versionCodename"),
                    "name": distro.get("name"),
                }
        return None
    except Exception:
        return None


def get_crane_path() -> Path:
    """Get the path to the crane binary."""
    system = platform.system().lower()
    arch = platform.machine().lower()

    if system == "darwin" and arch == "arm64":
        plat = "darwin-arm64"
    elif system == "linux" and arch in ("x86_64", "amd64"):
        plat = "linux-amd64"
    elif system == "linux" and arch in ("arm64", "aarch64"):
        plat = "linux-arm64"
    else:
        plat = f"{system}-{arch}"

    return Path(__file__).parent.parent / "bin" / plat / "crane"


def resolve_image_digest(image_ref: str) -> str | None:
    """Resolve an image reference to its digest using crane.

    Args:
        image_ref: Image reference like "ubuntu:24.04"

    Returns:
        Full digest like "sha256:c35e29c9..." or None if failed
    """
    crane = get_crane_path()
    if not crane.exists():
        return None

    try:
        result = subprocess.run(
            [str(crane), "digest", image_ref],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


# Cache for Ubuntu series data
_series_cache: dict[str, str] | None = None


def get_ubuntu_codename(version: str) -> str:
    """Get Ubuntu codename from version number using Launchpad API.

    Args:
        version: Ubuntu version like "24.04" or "22.04"

    Returns:
        Codename like "noble" or "jammy"

    Raises:
        ValueError: If version not found
    """
    global _series_cache

    if _series_cache is None:
        url = "https://api.launchpad.net/1.0/ubuntu/series"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode())
            _series_cache = {
                entry["version"]: entry["name"]
                for entry in data["entries"]
                if entry.get("version")
            }

    if version not in _series_cache:
        raise ValueError(f"Unknown Ubuntu version: {version}")

    return _series_cache[version]


def get_package_version(package: str, codename: str) -> str | None:
    """Get latest package version from Ubuntu packages website.

    Args:
        package: Package name like "curl"
        codename: Ubuntu codename like "noble"

    Returns:
        Version string like "8.5.0-2ubuntu10.6" or None if not found
    """
    # Query packages.ubuntu.com for binary package info
    url = f"https://packages.ubuntu.com/{codename}/{package}"

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            html = response.read().decode()

            # Extract version from page title or content
            # Format: "Package: curl (8.5.0-2ubuntu10.6 and others)"
            # or "Package: gnupg (2.4.4-2ubuntu17.3)"
            match = re.search(r"Package:\s*\S+\s*\(([^)]+)\)", html)
            if match:
                version_text = match.group(1)
                # Handle "X.Y.Z and others" format - take first version
                version = version_text.split(" and ")[0].strip()
                return version

            return None

    except Exception:
        return None


def extract_packages_from_dockerfile(dockerfile_content: str) -> list[str]:
    """Extract package names from apt-get install commands.

    Args:
        dockerfile_content: Content of Dockerfile

    Returns:
        List of package names
    """
    packages = []

    # Match apt-get install commands (handles multiline with backslash)
    # First, normalize line continuations
    content = dockerfile_content.replace("\\\n", " ")

    # Pattern for apt-get install
    pattern = r"apt-get\s+install\s+(?:-[a-zA-Z]+\s+)*(.+?)(?:\s*&&|\s*$|\s*;)"

    for match in re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE):
        pkg_string = match.group(1)
        # Split on whitespace and filter out flags
        for token in pkg_string.split():
            token = token.strip()
            # Skip flags, version specifiers, and empty tokens
            if token and not token.startswith("-") and "=" not in token:
                packages.append(token)

    return packages


def extract_base_image(dockerfile_content: str) -> tuple[str, str] | None:
    """Extract base image from FROM line.

    Args:
        dockerfile_content: Content of Dockerfile

    Returns:
        Tuple of (image, tag) or None if not found.
        For digest references (image@sha256:...), returns (image, digest).
    """
    # Match FROM line - handle both tag (:) and digest (@) formats
    # FROM ubuntu:24.04 -> ("ubuntu", "24.04")
    # FROM ubuntu@sha256:abc123 -> ("ubuntu", "sha256:abc123")
    pattern = r"^FROM\s+([^\s:@]+)(?:[:@]([^\s]+))?(?:\s+AS\s+\w+)?$"

    for line in dockerfile_content.splitlines():
        line = line.strip()
        match = re.match(pattern, line, re.IGNORECASE)
        if match:
            image = match.group(1)
            tag_or_digest = match.group(2) or "latest"
            return (image, tag_or_digest)

    return None


def resolve_ubuntu_version(dockerfile_path: Path, images_dir: Path, lock_path: Path | None = None) -> tuple[str, str] | None:
    """Resolve Ubuntu version from base image.

    For local base images, inspects the built image tar with syft.
    For ubuntu directly, uses the tag version.

    Args:
        dockerfile_path: Path to generated Dockerfile
        images_dir: Path to images directory for resolving local bases
        lock_path: Optional path to lock file (for extracting version from digest)

    Returns:
        Tuple of (version, codename) or None if not Ubuntu-based
    """
    content = dockerfile_path.read_text()
    base = extract_base_image(content)

    if not base:
        return None

    image, tag_or_digest = base

    # Check if it's Ubuntu directly
    if image == "ubuntu":
        # Handle digest reference - need to look up original version from lock file
        if tag_or_digest.startswith("sha256:"):
            if lock_path and lock_path.exists():
                data = yaml.safe_load(lock_path.read_text())
                if data:
                    meta = data.get("_meta", {})
                    base_info = meta.get("base", {})
                    if isinstance(base_info, dict):
                        original = base_info.get("original", "")
                        if original.startswith("ubuntu:"):
                            version = original.split(":", 1)[1]
                            codename = meta.get("codename")
                            if codename:
                                return (version, codename)
            return None

        codename = get_ubuntu_codename(tag_or_digest)
        return (tag_or_digest, codename)

    # Check if it's a local image - inspect the built image tar with syft
    # Local images are referenced by name only (no registry prefix)
    if "/" not in image and image not in ("ubuntu", "alpine", "debian"):
        dist_dir = dockerfile_path.parent.parent.parent
        base_image_tar = dist_dir / image / tag_or_digest / "image.tar"

        if base_image_tar.exists():
            distro = extract_distro_from_image(base_image_tar)
            if distro and distro.get("id") == "ubuntu":
                version_id = distro.get("versionID")
                codename = distro.get("versionCodename")
                if version_id and codename:
                    return (version_id, codename)

    return None


def read_lock_file(lock_path: Path) -> dict[str, str]:
    """Read packages.lock file.

    Args:
        lock_path: Path to packages.lock

    Returns:
        Dict of package -> version
    """
    if not lock_path.exists():
        return {}

    data = yaml.safe_load(lock_path.read_text())
    return data.get("packages", {}) if data else {}


def read_base_digest(lock_path: Path) -> tuple[str, str] | None:
    """Read base image digest from lock file.

    Args:
        lock_path: Path to packages.lock

    Returns:
        Tuple of (original_ref, digest) or None if not available
    """
    if not lock_path.exists():
        return None

    data = yaml.safe_load(lock_path.read_text())
    if not data:
        return None

    meta = data.get("_meta", {})
    base = meta.get("base")

    if isinstance(base, dict):
        original = base.get("original")
        digest = base.get("digest")
        if original and digest:
            return (original, digest)

    return None


def write_lock_file(
    lock_path: Path,
    packages: dict[str, str],
    base_image: str,
    base_digest: str | None,
    codename: str,
) -> None:
    """Write packages.lock file.

    Args:
        lock_path: Path to packages.lock
        packages: Dict of package -> version
        base_image: Base image reference
        base_digest: Digest of base image (sha256:...)
        codename: Ubuntu codename
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    base_info: dict[str, str] = {"original": base_image}
    if base_digest:
        base_info["digest"] = base_digest

    content = {
        "_meta": {
            "generated_by": "image-manager lock",
            "source": "packages.ubuntu.com",
            "base": base_info,
            "codename": codename,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        "packages": packages,
    }

    lock_path.write_text(yaml.dump(content, default_flow_style=False, sort_keys=False))


def rewrite_apt_install(dockerfile_content: str, packages: dict[str, str]) -> str:
    """Rewrite apt-get install commands to pin package versions.

    Args:
        dockerfile_content: Original Dockerfile content
        packages: Dict of package -> version

    Returns:
        Modified Dockerfile content with pinned versions
    """
    result = dockerfile_content

    for package, version in packages.items():
        # Replace package name with pinned version
        # Match package name not already pinned (not followed by =)
        pattern = rf"(?<![=\w-]){re.escape(package)}(?![=\w-])"
        replacement = f"{package}={version}"
        result = re.sub(pattern, replacement, result)

    return result


def rewrite_from_digest(
    dockerfile_content: str,
    original_ref: str,
    digest: str,
) -> str:
    """Rewrite FROM statement to use digest instead of tag.

    Args:
        dockerfile_content: Original Dockerfile content
        original_ref: Original image reference like "ubuntu:24.04"
        digest: Full digest like "sha256:abc123..."

    Returns:
        Modified Dockerfile with digest-pinned FROM
    """
    # Parse original reference
    if ":" in original_ref:
        image, tag = original_ref.split(":", 1)
    else:
        image = original_ref
        tag = "latest"

    # Pattern to match FROM with optional AS clause
    # FROM ubuntu:24.04 -> FROM ubuntu@sha256:abc123
    # FROM ubuntu:24.04 AS builder -> FROM ubuntu@sha256:abc123 AS builder
    pattern = rf"^(FROM\s+){re.escape(image)}:{re.escape(tag)}(\s+AS\s+\w+)?$"
    replacement = rf"\g<1>{image}@{digest}\g<2>"

    return re.sub(pattern, replacement, dockerfile_content, flags=re.MULTILINE | re.IGNORECASE)


def run_lock(
    image_ref: str,
    images_dir: Path,
    dist_dir: Path,
) -> int:
    """Generate packages.lock for an image.

    Args:
        image_ref: Image reference like "base:2025.09"
        images_dir: Path to images directory
        dist_dir: Path to dist directory with generated Dockerfiles

    Returns:
        Exit code (0 for success)
    """
    name, tag = image_ref.split(":")

    # Find the generated Dockerfile
    dockerfile_path = dist_dir / name / tag / "Dockerfile"
    if not dockerfile_path.exists():
        print(f"Error: Generated Dockerfile not found: {dockerfile_path}")
        print("Run 'image-manager generate' first")
        return 1

    # Determine lock file path first - we may need it for resolving digest references
    # Find the image source directory
    image_dir = None
    for subdir in images_dir.rglob("image.yml"):
        # Check if this is the right image
        parent = subdir.parent
        if parent.parent.name == name or (parent.parent.parent.exists() and parent.parent.parent.name == name):
            image_dir = parent
            break

    if not image_dir:
        # Fallback: use dist directory
        image_dir = dist_dir / name / tag

    lock_path = image_dir / "packages.lock"

    # Resolve Ubuntu version (pass lock_path to handle digest references)
    ubuntu_info = resolve_ubuntu_version(dockerfile_path, images_dir, lock_path)
    if not ubuntu_info:
        print(f"Error: Could not determine Ubuntu version for {image_ref}")
        print("Package locking currently only supports Ubuntu-based images")
        return 1

    ubuntu_version, codename = ubuntu_info
    print(f"Detected Ubuntu {ubuntu_version} ({codename})")

    # Check if lock file already has packages (use existing versions)
    existing_packages = read_lock_file(lock_path)
    if existing_packages:
        print(f"Using {len(existing_packages)} existing locked packages")
        locked = existing_packages
    else:
        # Extract packages from Dockerfile
        content = dockerfile_path.read_text()
        packages = extract_packages_from_dockerfile(content)

        if not packages:
            print("No apt-get install commands found in Dockerfile")
            print("Hint: Run 'image-manager generate' first, then lock before adding version pins")
            return 0

        print(f"Found {len(packages)} packages: {', '.join(packages)}")

        # Resolve versions
        locked = {}
        for pkg in packages:
            print(f"  Resolving {pkg}...", end=" ", flush=True)
            version = get_package_version(pkg, codename)
            if version:
                locked[pkg] = version
                print(f"{version}")
            else:
                print("NOT FOUND")

        if not locked:
            print("Error: Could not resolve any package versions")
            return 1

    # Resolve base image digest
    base_image_ref = f"ubuntu:{ubuntu_version}"
    print(f"Resolving digest for {base_image_ref}...", end=" ", flush=True)
    base_digest = resolve_image_digest(base_image_ref)
    if base_digest:
        print(f"{base_digest[:19]}...")
    else:
        print("FAILED (will use tag only)")

    write_lock_file(lock_path, locked, base_image_ref, base_digest, codename)
    print(f"\nWrote {lock_path}")

    return 0
