"""Configuration loading from .image-manager.yml."""

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_yaml import parse_yaml_file_as

_config_cache: dict | None = None

CONFIG_FILE = ".image-manager.yml"


def expand_env_vars(value: str | None) -> str | None:
    """Expand ${VAR} references in a string value.

    Returns None if the value is None or contains an undefined env var.
    """
    if value is None:
        return None

    if not value:
        return value

    # Check if it's a pure env var reference like ${VAR}
    match = re.fullmatch(r'\$\{([^}]+)\}', value)
    if match:
        var_name = match.group(1)
        return os.environ.get(var_name)

    # No env var pattern found, return as-is
    return value


def load_config() -> dict:
    """Load .image-manager.yml from current directory.

    Returns empty dict if file doesn't exist or is empty.
    Result is cached for the duration of the process.
    """
    global _config_cache

    if _config_cache is not None:
        return _config_cache

    config_path = Path.cwd() / CONFIG_FILE

    if not config_path.exists():
        _config_cache = {}
        return _config_cache

    try:
        content = config_path.read_text()
        _config_cache = yaml.safe_load(content) or {}
    except Exception:
        _config_cache = {}

    return _config_cache


class TagConfig(BaseModel):
    """Configuration for a single tag"""
    name: str
    versions: dict[str, str] = {}
    variables: dict[str, str] = {}
    rootfs_user: str | None = None
    rootfs_copy: bool | None = None


class VariantConfig(BaseModel):
    """Configuration for a variant"""
    name: str
    tag_suffix: str
    template: str | None = None
    versions: dict[str, str] = {}
    variables: dict[str, str] = {}
    rootfs_user: str | None = None
    rootfs_copy: bool | None = None


class ImageConfig(BaseModel):
    """Root configuration from image.yml"""
    name: str | None = None
    template: str | None = None
    versions: dict[str, str] = {}
    variables: dict[str, str] = {}
    tags: list[TagConfig]
    variants: list[VariantConfig] = []
    is_base_image: bool = False
    extends: str | None = None
    aliases: dict[str, str] = {}
    rootfs_user: str | None = None
    rootfs_copy: bool | None = None


class ConfigLoader:
    """Loads and validates image.yml files"""

    @staticmethod
    def load(path: Path) -> ImageConfig:
        """Load and validate an image.yml file"""
        return parse_yaml_file_as(ImageConfig, path)
