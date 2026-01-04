from pathlib import Path


class TemplateResolver:
    """Resolves template paths using convention and discovery"""

    def resolve(
        self,
        templates_dir: Path,
        explicit: str | None,
        variant_name: str | None
    ) -> Path:
        """
        Resolve template path following discovery order:
        1. Explicit template if specified
        2. Variant-specific template (Dockerfile.{variant}.jinja2)
        3. Default template (Dockerfile.jinja2)

        Raises FileNotFoundError if no template found.
        """
        # Try explicit first
        if explicit:
            path = templates_dir / explicit
            if path.exists():
                return path
            raise FileNotFoundError(
                f"Template not found: explicit template '{explicit}' "
                f"does not exist in {templates_dir}"
            )

        # Try variant-specific
        if variant_name:
            variant_path = templates_dir / f"Dockerfile.{variant_name}.jinja2"
            if variant_path.exists():
                return variant_path

        # Fall back to default
        default_path = templates_dir / "Dockerfile.jinja2"
        if default_path.exists():
            return default_path

        # Nothing found
        searched = ["Dockerfile.jinja2"]
        if variant_name:
            searched.insert(0, f"Dockerfile.{variant_name}.jinja2")

        raise FileNotFoundError(
            f"Template not found. Searched in {templates_dir}: {', '.join(searched)}"
        )
