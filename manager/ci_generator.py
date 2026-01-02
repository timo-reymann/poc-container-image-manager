"""CI configuration generator for GitLab and other providers."""

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from manager.dependency_graph import extract_dependencies

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


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


def build_ci_context(images: list) -> dict:
    """Build context dictionary for CI templates.

    Args:
        images: List of Image objects (should be in dependency order)

    Returns:
        Dictionary with images, platforms, and metadata for templates
    """
    dependencies = extract_dependencies(images)
    image_names = {img.name for img in images}
    depths = _calculate_depths(image_names, dependencies)
    max_depth = max(depths.values()) if depths else 0

    image_contexts = []
    for image in images:
        # Get direct dependencies (other images this one depends on)
        deps = [d for d in dependencies.get(image.name, set())
                if d in image_names]

        # Collect all tag names including variant tags
        tag_names = [tag.name for tag in image.tags]

        image_contexts.append({
            "name": image.name,
            "dependencies": sorted(deps),
            "tags": tag_names,
            "depth": depths[image.name],
        })

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
    }


def generate_gitlab_ci(images: list, output_path: Path) -> None:
    """Generate GitLab CI configuration file.

    Args:
        images: List of Image objects (should be in dependency order)
        output_path: Path to write the generated CI config
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR / "gitlab"),
        keep_trailing_newline=True,
    )
    template = env.get_template("pipeline.yml.j2")

    context = build_ci_context(images)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(**context))
