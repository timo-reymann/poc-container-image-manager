"""Rootfs merging utilities for image builds."""

import shutil
from pathlib import Path


def collect_rootfs_paths(image_path: Path, version_path: Path, variant_name: str | None) -> list[Path]:
    """Collect rootfs directories from all levels in merge order.

    Order (later wins): image -> version -> variant

    Args:
        image_path: Path to image directory (e.g., images/python)
        version_path: Path to version directory (e.g., images/python/3)
        variant_name: Optional variant name

    Returns:
        List of existing rootfs paths in merge order
    """
    paths = []

    # Level 1: Image-wide rootfs
    image_rootfs = image_path / "rootfs"
    if image_rootfs.is_dir():
        paths.append(image_rootfs)

    # Level 2: Version-specific rootfs
    version_rootfs = version_path / "rootfs"
    if version_rootfs.is_dir():
        paths.append(version_rootfs)

    # Level 3: Variant-specific rootfs
    if variant_name:
        variant_rootfs = version_path / variant_name / "rootfs"
        if variant_rootfs.is_dir():
            paths.append(variant_rootfs)

    return paths


def has_rootfs_content(rootfs_paths: list[Path]) -> bool:
    """Check if any rootfs directory contains actual files (not just empty dirs).

    Args:
        rootfs_paths: List of rootfs directories to check

    Returns:
        True if any directory contains files
    """
    for rootfs_path in rootfs_paths:
        if not rootfs_path.exists():
            continue
        for item in rootfs_path.rglob("*"):
            if item.is_file() or item.is_symlink():
                return True
    return False


def merge_rootfs(rootfs_paths: list[Path], dest: Path) -> None:
    """Merge multiple rootfs directories into destination.

    Later directories in the list override earlier ones (later wins).
    Preserves symlinks.

    Args:
        rootfs_paths: List of rootfs directories in merge order
        dest: Destination directory for merged rootfs
    """
    if not rootfs_paths:
        return

    dest.mkdir(parents=True, exist_ok=True)

    for rootfs_path in rootfs_paths:
        if not rootfs_path.exists():
            continue

        for item in rootfs_path.rglob("*"):
            rel_path = item.relative_to(rootfs_path)
            dest_path = dest / rel_path

            if item.is_symlink():
                # Preserve symlinks
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                if dest_path.exists() or dest_path.is_symlink():
                    dest_path.unlink()
                dest_path.symlink_to(item.readlink())
            elif item.is_file():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest_path)
            elif item.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)


def warn_sensitive_files(rootfs_path: Path) -> list[str]:
    """Check for potentially sensitive files in rootfs.

    Args:
        rootfs_path: Path to rootfs directory

    Returns:
        List of warning messages for sensitive files found
    """
    sensitive_patterns = [".env", "*.key", "*.pem", "*.p12", "*.pfx", "id_rsa", "id_ed25519"]
    warnings = []

    if not rootfs_path.exists():
        return warnings

    for pattern in sensitive_patterns:
        for match in rootfs_path.rglob(pattern):
            warnings.append(f"Warning: potentially sensitive file in rootfs: {match}")

    return warnings
