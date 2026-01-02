"""CI configuration generator for GitLab and other providers."""

from datetime import datetime, timezone
from pathlib import Path
from manager.dependency_graph import extract_dependencies


def build_ci_context(images: list) -> dict:
    """Build context dictionary for CI templates.

    Args:
        images: List of Image objects (should be in dependency order)

    Returns:
        Dictionary with images, platforms, and metadata for templates
    """
    dependencies = extract_dependencies(images)

    image_contexts = []
    for image in images:
        # Get direct dependencies (other images this one depends on)
        deps = [d for d in dependencies.get(image.name, set())
                if d in {img.name for img in images}]

        # Collect all tag names including variant tags
        tag_names = [tag.name for tag in image.tags]

        image_contexts.append({
            "name": image.name,
            "dependencies": sorted(deps),
            "tags": tag_names,
        })

    return {
        "images": image_contexts,
        "platforms": ["amd64", "arm64"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
