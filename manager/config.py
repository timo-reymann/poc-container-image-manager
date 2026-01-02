from pathlib import Path
from pydantic import BaseModel
from pydantic_yaml import parse_yaml_file_as


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
