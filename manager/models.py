from pathlib import Path
from dataclasses import dataclass, field

from manager.config import ImageConfig
from manager.template_resolver import TemplateResolver
from manager.tag_generator import TagGenerator
from manager.merger import Merger


@dataclass
class Tag:
    """Resolved tag with merged versions and variables"""
    name: str
    versions: dict[str, str]
    variables: dict[str, str]
    rootfs_user: str = "0:0"
    rootfs_copy: bool = True


@dataclass
class Variant:
    """Resolved variant with generated tags"""
    name: str
    template_path: Path
    tags: list[Tag]
    aliases: dict[str, str] = field(default_factory=dict)
    rootfs_user: str = "0:0"
    rootfs_copy: bool = True


@dataclass
class Image:
    """Fully resolved image with all computed data"""
    name: str
    path: Path
    template_path: Path
    versions: dict[str, str]
    variables: dict[str, str]
    tags: list[Tag]
    variants: list[Variant]
    is_base_image: bool
    extends: str | None
    aliases: dict[str, str]
    rootfs_user: str = "0:0"
    rootfs_copy: bool = True

    @property
    def root(self) -> Path:
        """Compatibility property for old code"""
        return self.path

    @property
    def dockerfile_template_path(self) -> Path:
        """Compatibility property for old code"""
        return self.template_path

    @property
    def test_config_path(self) -> Path:
        """Path to test config template"""
        return self.path / "test.yml.jinja2"

    @property
    def full_qualified_base_image_name(self) -> str | None:
        """Get fully qualified base image name"""
        if not self.is_base_image:
            return None

        if len(self.tags) != 1:
            return None

        return f"{self.name}:{self.tags[0].name}"


class ModelResolver:
    """Transforms config objects into resolved domain models"""

    def __init__(self):
        self.template_resolver = TemplateResolver()

    def resolve(self, config: ImageConfig, path: Path) -> Image:
        """
        Resolve a config into a fully computed Image model.

        Args:
            config: Loaded configuration
            path: Path to the image directory (e.g., images/python/3/)

        Returns:
            Fully resolved Image with all data computed
        """
        # Try templates in sibling templates/ directory first, fall back to same directory
        templates_parent_dir = path.parent / "templates"
        templates_dir = templates_parent_dir if templates_parent_dir.exists() else path

        # Resolve base template
        template_path = self.template_resolver.resolve(
            templates_dir=templates_dir,
            explicit=config.template,
            variant_name=None
        )

        # Build base tags with merged data
        base_tags = []
        for tag_config in config.tags:
            merged_versions = Merger.merge(config.versions, tag_config.versions)
            merged_variables = Merger.merge(config.variables, tag_config.variables)

            # Inherit rootfs settings: image -> tag (later wins)
            tag_rootfs_user = tag_config.rootfs_user if tag_config.rootfs_user is not None else (config.rootfs_user or "0:0")
            tag_rootfs_copy = tag_config.rootfs_copy if tag_config.rootfs_copy is not None else (config.rootfs_copy if config.rootfs_copy is not None else True)

            base_tags.append(Tag(
                name=tag_config.name,
                versions=merged_versions,
                variables=merged_variables,
                rootfs_user=tag_rootfs_user,
                rootfs_copy=tag_rootfs_copy
            ))

        # Generate automatic semver aliases from base tags
        from manager.alias_generator import generate_semver_aliases
        aliases = generate_semver_aliases(base_tags)

        # Build variants with generated tags
        variants = []
        for variant_config in config.variants:
            # Resolve variant template
            variant_template_path = self.template_resolver.resolve(
                templates_dir=templates_dir,
                explicit=variant_config.template,
                variant_name=variant_config.name
            )

            # Generate variant tags
            variant_tags = TagGenerator.generate_variant_tags(
                base_tags=base_tags,
                variant=variant_config,
                image_versions=config.versions,
                image_variables=config.variables
            )

            # Generate aliases for variant tags
            variant_aliases = generate_semver_aliases(variant_tags)

            # Also create variant aliases from base aliases
            # e.g., if base has 9 → 9.0.300, variant gets 9-semantic → 9.0.300-semantic
            suffix = variant_config.tag_suffix
            for base_alias, base_target in aliases.items():
                variant_alias = f"{base_alias}{suffix}"
                variant_target = f"{base_target}{suffix}"
                variant_aliases[variant_alias] = variant_target

            # Inherit rootfs settings for variant: image -> variant (later wins)
            variant_rootfs_user = variant_config.rootfs_user if variant_config.rootfs_user is not None else (config.rootfs_user or "0:0")
            variant_rootfs_copy = variant_config.rootfs_copy if variant_config.rootfs_copy is not None else (config.rootfs_copy if config.rootfs_copy is not None else True)

            variants.append(Variant(
                name=variant_config.name,
                template_path=variant_template_path,
                tags=variant_tags,
                aliases=variant_aliases,  # Add variant-specific aliases
                rootfs_user=variant_rootfs_user,
                rootfs_copy=variant_rootfs_copy
            ))

        # Smart name detection
        if config.name:
            # Explicit config always wins
            image_name = config.name
        elif config.is_base_image or "base" in path.parts:
            # Base images: use directory name
            # images/base/ubuntu/ → "ubuntu"
            image_name = path.name
        else:
            # Regular images: use parent directory name
            # images/dotnet/9.0/ → "dotnet"
            image_name = path.parent.name

        # Image-level rootfs settings (with defaults)
        image_rootfs_user = config.rootfs_user or "0:0"
        image_rootfs_copy = config.rootfs_copy if config.rootfs_copy is not None else True

        return Image(
            name=image_name,
            path=path,
            template_path=template_path,
            versions=config.versions,
            variables=config.variables,
            tags=base_tags,
            variants=variants,
            is_base_image=config.is_base_image,
            extends=config.extends,
            aliases=aliases,  # Use generated aliases instead of config.aliases
            rootfs_user=image_rootfs_user,
            rootfs_copy=image_rootfs_copy
        )
