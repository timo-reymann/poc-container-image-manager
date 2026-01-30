"""CI configuration generator for GitLab and other providers."""

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from manager.dependency_graph import extract_dependencies

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
MAIN_TEMPLATE_NAME = "pipeline.yml.j2"


def _calculate_depths(image_names: set[str], dependencies: dict[str, set[str]]) -> dict[str, int]:
    """Calculate dependency depth for each image.

    Depth 0 = no dependencies, depth 1 = depends only on depth-0 images, etc.
    """
    depths = {}
    remaining = set(image_names)

    current_depth = 0
    while remaining:
        # Find images whose dependencies are all already assigned depths
        ready = set()
        for name in remaining:
            deps = dependencies.get(name, set()) & image_names
            if all(d in depths for d in deps):
                ready.add(name)

        if not ready:
            # Circular dependency or bug - assign remaining to current depth
            for name in remaining:
                depths[name] = current_depth
            break

        for name in ready:
            depths[name] = current_depth
            remaining.remove(name)

        current_depth += 1

    return depths


def build_ci_context(images: list, artifacts: bool = False) -> dict:
    """Build context dictionary for CI templates.

    Args:
        images: List of Image objects (should be in dependency order)
        artifacts: Whether to enable artifact passing between jobs (default: False)
                   When False, jobs use the registry directly for image transfer.
                   When True, jobs upload/download artifacts (can be GB+ in size).

    Returns:
        Dictionary with images, platforms, and metadata for templates
    """
    dependencies = extract_dependencies(images)
    image_names = {img.name for img in images}
    depths = _calculate_depths(image_names, dependencies)
    max_depth = max(depths.values()) if depths else 0

    # Deduplicate images by name (keep first occurrence, merge tags)
    seen_names = {}
    for image in images:
        if image.name in seen_names:
            # Merge tags from duplicate image
            seen_names[image.name]["tags"].extend([tag.name for tag in image.tags])
            continue

        # Get direct dependencies (other images this one depends on)
        deps = [d for d in dependencies.get(image.name, set())
                if d in image_names]

        # Collect all tag names including variant tags
        tag_names = [tag.name for tag in image.tags]

        seen_names[image.name] = {
            "name": image.name,
            "dependencies": sorted(deps),
            "tags": tag_names,
            "depth": depths[image.name],
        }

    image_contexts = list(seen_names.values())

    # Sort images by depth, then by name for consistent ordering
    sorted_by_depth = sorted(image_contexts, key=lambda x: (x["depth"], x["name"]))

    # Generate stage names: build-<image>, manifest-<image> for each image in order
    stages = []
    for img in sorted_by_depth:
        stages.append(f"build-{img['name']}")
        stages.append(f"manifest-{img['name']}")
    stages.append("test")

    return {
        "images": image_contexts,
        "platforms": ["amd64", "arm64"],
        "stages": stages,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
    }


def generate_gitlab_ci(images: list, output_path: Path, artifacts: bool = False) -> None:
    """Generate GitLab CI configuration file.

    Args:
        images: List of Image objects (should be in dependency order)
        output_path: Path to write the generated CI config
        artifacts: Whether to enable artifact passing between jobs
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR / "gitlab"),
        keep_trailing_newline=True,
    )
    template = env.get_template("pipeline.yml.j2")

    context = build_ci_context(images, artifacts=artifacts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(**context))


def generate_github_ci(images: list, output_path: Path, artifacts: bool = False) -> None:
    """Generate GitHub Actions workflow file.

    Args:
        images: List of Image objects (should be in dependency order)
        output_path: Path to write the generated workflow
        artifacts: Whether to enable artifact passing between jobs
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR / "github"),
        keep_trailing_newline=True,
    )
    template = env.get_template("workflow.yml.j2")

    context = build_ci_context(images, artifacts=artifacts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(**context))


def build_extended_context(images: list, artifacts: bool = False) -> dict:
    """Build extended context dictionary for custom CI templates.

    Includes all standard context plus configuration values.

    Args:
        images: List of Image objects (should be in dependency order)
        artifacts: Whether to enable artifact passing between jobs

    Returns:
        Dictionary with images, platforms, metadata, and config for templates
    """
    from manager.config import (
        get_registry_url,
        get_registries,
        get_cache_config,
        get_labels_config,
    )

    # Start with standard context
    context = build_ci_context(images, artifacts=artifacts)

    # Add config section with registry, cache, and labels info
    registries = get_registries()
    cache_config = get_cache_config()
    labels_config = get_labels_config()

    context["config"] = {
        "registry": get_registry_url(),
        "registries": [
            {
                "url": reg.url,
                "default": reg.default,
                "insecure": reg.insecure,
            }
            for reg in registries
        ],
        "cache": {
            "endpoint": cache_config.endpoint,
            "bucket": cache_config.bucket,
            "region": cache_config.region,
        } if cache_config else None,
        "labels": {
            "vendor": labels_config.vendor,
            "authors": labels_config.authors,
            "url": labels_config.url,
            "documentation": labels_config.documentation,
            "licenses": labels_config.licenses,
        },
    }

    return context


def generate_custom_ci(
    images: list, template_dir: Path, output_path: Path, artifacts: bool = False
) -> None:
    """Generate CI configuration from a custom template directory.

    The template directory should contain a main template file named 'pipeline.yml.j2'
    and can include additional templates that can be included using Jinja2's include.

    Args:
        images: List of Image objects (should be in dependency order)
        template_dir: Path to directory containing Jinja2 templates
        output_path: Path to write the generated CI config
        artifacts: Whether to enable artifact passing between jobs

    Raises:
        FileNotFoundError: If template_dir doesn't exist or missing main template
    """
    template_dir = Path(template_dir)

    if not template_dir.exists():
        raise FileNotFoundError(f"Template directory not found: {template_dir}")

    main_template = template_dir / MAIN_TEMPLATE_NAME
    if not main_template.exists():
        raise FileNotFoundError(
            f"Main template '{MAIN_TEMPLATE_NAME}' not found in {template_dir}"
        )

    env = Environment(
        loader=FileSystemLoader(template_dir),
        keep_trailing_newline=True,
    )
    template = env.get_template(MAIN_TEMPLATE_NAME)

    context = build_extended_context(images, artifacts=artifacts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(**context))
