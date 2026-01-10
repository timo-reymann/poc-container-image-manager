"""Package locking for reproducible builds."""

import platform
import re
import subprocess
import urllib.request
import urllib.parse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _get_bin_platform() -> str:
    """Get the platform directory for bundled binaries."""
    system = platform.system().lower()
    arch = platform.machine().lower()

    if system == "darwin" and arch == "arm64":
        return "darwin-arm64"
    elif system == "linux" and arch in ("x86_64", "amd64"):
        return "linux-amd64"
    elif system == "linux" and arch in ("arm64", "aarch64"):
        return "linux-arm64"
    else:
        return f"{system}-{arch}"


def get_crane_path() -> Path:
    """Get the path to the crane binary."""
    return Path(__file__).parent.parent / "bin" / _get_bin_platform() / "crane"


def get_syft_path() -> Path:
    """Get the path to the syft binary."""
    return Path(__file__).parent.parent / "bin" / _get_bin_platform() / "syft"


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
    """Extract effective base image from Dockerfile (last FROM line).

    In multi-stage builds, only the last FROM determines the final image's base.

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

    last_match = None
    for line in dockerfile_content.splitlines():
        line = line.strip()
        match = re.match(pattern, line, re.IGNORECASE)
        if match:
            image = match.group(1)
            tag_or_digest = match.group(2) or "latest"
            last_match = (image, tag_or_digest)

    return last_match


def read_lock_file(lock_path: Path, base_ref: str | None = None) -> dict[str, str]:
    """Read packages.lock file.

    Args:
        lock_path: Path to packages.lock
        base_ref: Base image ref to look up (e.g., 'ubuntu:24.04'). If None, returns
                  packages from legacy format or first base section.

    Returns:
        Dict of package -> version
    """
    if not lock_path.exists():
        return {}

    data = yaml.safe_load(lock_path.read_text())
    if not data:
        return {}

    # New multi-base format
    if "bases" in data:
        bases = data["bases"]
        if base_ref and base_ref in bases:
            return bases[base_ref].get("packages", {})
        # Return first base's packages if no specific base requested
        if bases:
            first_base = next(iter(bases.values()))
            return first_base.get("packages", {})
        return {}

    # Legacy single-base format
    return data.get("packages", {})


def read_base_digest(lock_path: Path, base_ref: str | None = None) -> tuple[str, str] | None:
    """Read base image digest from lock file.

    Args:
        lock_path: Path to packages.lock
        base_ref: Base image ref to look up (e.g., 'ubuntu:24.04')

    Returns:
        Tuple of (original_ref, digest) or None if not available
    """
    if not lock_path.exists():
        return None

    data = yaml.safe_load(lock_path.read_text())
    if not data:
        return None

    # New multi-base format
    if "bases" in data:
        bases = data["bases"]
        if base_ref and base_ref in bases:
            base_info = bases[base_ref]
            digest = base_info.get("digest")
            if digest:
                return (base_ref, digest)
        return None

    # Legacy single-base format
    meta = data.get("_meta", {})
    base = meta.get("base")

    if isinstance(base, dict):
        original = base.get("original")
        digest = base.get("digest")
        if original and digest:
            return (original, digest)

    return None


def read_all_bases(lock_path: Path) -> dict[str, dict]:
    """Read all base sections from lock file.

    Args:
        lock_path: Path to packages.lock

    Returns:
        Dict of base_ref -> {digest, codename, packages}
    """
    if not lock_path.exists():
        return {}

    data = yaml.safe_load(lock_path.read_text())
    if not data:
        return {}

    return data.get("bases", {})


def write_lock_file(
    lock_path: Path,
    bases: dict[str, dict],
) -> None:
    """Write packages.lock file with multiple base sections.

    Args:
        lock_path: Path to packages.lock
        bases: Dict of base_ref -> {digest, codename, packages}
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    content = {
        "_meta": {
            "generated_by": "image-manager lock",
            "source": "packages.ubuntu.com",
            "date": datetime.now(timezone.utc).isoformat(),
        },
        "bases": bases,
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


def _get_base_ref(dockerfile_path: Path, dist_dir: Path) -> str | None:
    """Get the effective Ubuntu base reference for a Dockerfile.

    Uses syft to inspect built base images and extract the actual Ubuntu version.
    Falls back to Dockerfile parsing if image tar not available.

    Args:
        dockerfile_path: Path to the Dockerfile
        dist_dir: Path to dist directory with built images

    Returns:
        Normalized ref like 'ubuntu:24.04' or None if not Ubuntu-based
    """
    content = dockerfile_path.read_text()
    base = extract_base_image(content)  # Gets last FROM (multi-stage aware)
    if not base:
        return None

    image, tag = base

    # Direct ubuntu reference
    if image == "ubuntu":
        if tag.startswith("sha256:"):
            # Digest reference - use syft on this image's tar to get version
            # The tar is in the same directory as the Dockerfile
            image_tar = dockerfile_path.parent / "image.tar"
            if image_tar.exists():
                distro = extract_distro_from_image(image_tar)
                if distro and distro.get("id") == "ubuntu":
                    version = distro.get("versionID")
                    if version:
                        return f"ubuntu:{version}"
            return None
        return f"ubuntu:{tag}"

    # Local image - use syft to inspect the built image tar
    if "/" not in image and image not in ("alpine", "debian"):
        image_tar = dist_dir / image / tag / "image.tar"
        if image_tar.exists():
            distro = extract_distro_from_image(image_tar)
            if distro and distro.get("id") == "ubuntu":
                version = distro.get("versionID")
                if version:
                    return f"ubuntu:{version}"

        # Fallback: follow Dockerfile chain if tar not available
        base_dockerfile = dist_dir / image / tag / "Dockerfile"
        if base_dockerfile.exists():
            return _get_base_ref(base_dockerfile, dist_dir)

    return None


def run_lock(
    image_refs: list[str],
    images_dir: Path,
    dist_dir: Path,
) -> int:
    """Generate packages.lock for an image, with sections per base.

    Args:
        image_refs: List of image references for the same image (e.g., ['python:3.13.7', 'python:3.13.6'])
        images_dir: Path to images directory
        dist_dir: Path to dist directory with generated Dockerfiles

    Returns:
        Exit code (0 for success)
    """
    if not image_refs:
        return 0

    # All refs should be for the same image
    name = image_refs[0].split(":")[0]
    first_tag = image_refs[0].split(":")[1]

    # Determine lock file path
    image_dir = None
    for subdir in images_dir.rglob("image.yml"):
        parent = subdir.parent
        if parent.parent.name == name or (parent.parent.parent.exists() and parent.parent.parent.name == name):
            image_dir = parent
            break

    if not image_dir:
        image_dir = dist_dir / name / first_tag

    lock_path = image_dir / "packages.lock"

    # Group tags by their base image
    base_to_tags: dict[str, list[str]] = {}
    for ref in image_refs:
        tag = ref.split(":")[1]
        dockerfile_path = dist_dir / name / tag / "Dockerfile"
        if not dockerfile_path.exists():
            continue

        base_ref = _get_base_ref(dockerfile_path, dist_dir)
        if not base_ref:
            print(f"  Warning: Could not determine base for {ref}, skipping")
            continue

        if base_ref not in base_to_tags:
            base_to_tags[base_ref] = []
        base_to_tags[base_ref].append(tag)

    if not base_to_tags:
        print(f"Error: No valid Dockerfiles found for {name}")
        return 1

    print(f"Found {len(base_to_tags)} base image(s):")
    for base_ref, tags in base_to_tags.items():
        print(f"  {base_ref}: {len(tags)} tags")

    # Check for existing lock data
    existing_bases = read_all_bases(lock_path)

    # Build lock sections per base
    bases_data: dict[str, dict] = {}

    for base_ref, tags in base_to_tags.items():
        print(f"\nProcessing {base_ref}...")

        # Get Ubuntu version from base ref
        ubuntu_version = base_ref.split(":")[1]
        try:
            codename = get_ubuntu_codename(ubuntu_version)
        except ValueError as e:
            print(f"  Error: {e}")
            continue

        print(f"  Ubuntu {ubuntu_version} ({codename})")

        # Check existing packages for this base
        if base_ref in existing_bases:
            existing_packages = existing_bases[base_ref].get("packages", {})
            if existing_packages:
                print(f"  Using {len(existing_packages)} existing locked packages")
                bases_data[base_ref] = existing_bases[base_ref]
                continue

        # Extract packages from all tags using this base
        all_packages = set()
        for tag in tags:
            path = dist_dir / name / tag / "Dockerfile"
            if path.exists():
                content = path.read_text()
                packages = extract_packages_from_dockerfile(content)
                if packages:
                    all_packages.update(packages)

        if not all_packages:
            print(f"  No packages found for {base_ref}")
            continue

        packages = sorted(all_packages)
        print(f"  {len(packages)} unique packages")

        # Resolve versions in parallel
        print("  Resolving versions...")
        locked = {}
        not_found = []

        def resolve_pkg(pkg: str) -> tuple[str, str | None]:
            return (pkg, get_package_version(pkg, codename))

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(resolve_pkg, pkg): pkg for pkg in packages}
            for future in as_completed(futures):
                pkg, version = future.result()
                if version:
                    locked[pkg] = version
                else:
                    not_found.append(pkg)

        if not_found:
            print(f"  Not found: {', '.join(not_found)}")

        # Resolve digest
        digest = resolve_image_digest(base_ref)
        if digest:
            print(f"  Digest: {digest[:19]}...")

        bases_data[base_ref] = {
            "digest": digest,
            "codename": codename,
            "packages": locked,
        }

    if not bases_data:
        print("Error: Could not resolve any packages")
        return 1

    write_lock_file(lock_path, bases_data)
    print(f"\nWrote {lock_path}")

    return 0
