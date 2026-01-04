"""
DEPRECATED: Use manager.config and manager.models instead.
This file kept temporarily for reference during migration.

This is the old model file that combined config loading and model definitions.
The new architecture separates these concerns:
- manager.config: Config layer with Pydantic models for validation
- manager.models: Domain models with resolved/computed data
- manager.template_resolver: Template discovery logic
- manager.merger: Variable/version merging utilities
- manager.tag_generator: Variant tag generation

This file will be removed once migration is fully complete.
"""
from pathlib import Path

from pydantic import BaseModel
from pydantic_yaml import parse_yaml_file_as

VersionsType = dict[str, str]
VariablesType = dict[str, str]
AliasesType = dict[str, str]

class Tag(BaseModel):
    name: str
    versions: VersionsType | None = {}
    variables: VariablesType | None = {}


class Variant(BaseModel):
    name: str
    variables: VariablesType | None = {}
    versions: VersionsType | None = {}


class ContainerImageDefinition(BaseModel):
    tags: list[Tag]
    variables: VariablesType | None = {}
    versions: VersionsType | None = {}
    variants: list[Variant] = []
    root: Path | None = None
    name: str | None = None
    is_base_image: bool = False
    extends: str | None = None
    aliases: AliasesType = {}

    @classmethod
    def load_from_file(cls, path: Path) -> "ContainerImageDefinition":
        definition = parse_yaml_file_as(ContainerImageDefinition, path)
        definition.root = path.parent
        definition.name = (
            path.parent.name if definition.name is None else definition.name
        )
        return definition

    @property
    def dockerfile_template_path(self) -> Path:
        return self.root.joinpath("Dockerfile.jinja2")

    @property
    def test_config_path(self):
        return self.root.joinpath("test.yml.jinja2")

    @property
    def full_qualified_base_image_name(self):
        if not self.is_base_image:
            return None

        if len(self.tags) != 1:
            return None

        return f"{self.name}:{self.tags[0].name}"

    def get_latest_tag_for_alias(self, alias: str) -> str | None:
        pattern = self.aliases.get(alias, None)
        if not pattern:
            return None

        pattern_parts = pattern.split(".")
        relevant_tags = []
        for tag in self.tags:
            tag_parts = tag.name.split(".")
            include_tag = _matches_pattern(pattern_parts, tag_parts)
            if not include_tag:
                continue
            relevant_tags.append(tag_parts)

        if len(relevant_tags) < 1:
            return None

        return ".".join(sorted(relevant_tags, reverse=True)[0])

def _matches_pattern(pattern_parts: list[str], to_check_parts: list[str]) -> bool:
    for idx in range(len(pattern_parts)):
        if pattern_parts[idx] != to_check_parts[idx] and pattern_parts[idx] != "*":
            return False
    return True