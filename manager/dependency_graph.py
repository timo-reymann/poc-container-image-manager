import re
from typing import Set
from pathlib import Path
from graphlib import TopologicalSorter


class CyclicDependencyError(Exception):
    """Raised when a circular dependency is detected in the image dependency graph."""
    pass


def extract_base_image_refs(template_content: str) -> Set[str]:
    """
    Extract base image references from Jinja2 template.

    Matches patterns like:
    - {{ "name" | resolve_base_image }}
    - {{ 'name' | resolve_base_image }}

    Args:
        template_content: The Jinja2 template content

    Returns:
        Set of base image names referenced in the template
    """
    pattern = r'\{\{\s*["\']([^"\']+)["\']\s*\|\s*resolve_base_image\s*\}\}'
    matches = re.findall(pattern, template_content)
    return set(matches)


def extract_dependencies(images: list) -> dict[str, Set[str]]:
    """
    Extract dependencies from a list of Image objects.

    Dependencies are identified from:
    1. resolve_base_image references in main Dockerfile template
    2. resolve_base_image references in variant templates
    3. The extends field in image config

    Args:
        images: List of Image objects to analyze

    Returns:
        Dictionary mapping image name to set of dependencies (other image names it depends on)
    """
    dependencies = {}

    for image in images:
        deps = set()

        # Parse main template for base image references
        template_path = image.template_path
        if template_path.exists():
            template_content = template_path.read_text()
            # Extract base image references from the template
            deps.update(extract_base_image_refs(template_content))

        # Parse variant templates
        for variant in image.variants:
            if variant.template_path.exists():
                variant_content = variant.template_path.read_text()
                deps.update(extract_base_image_refs(variant_content))

        # Include extends field if present
        if image.extends:
            deps.add(image.extends)

        dependencies[image.name] = deps

    return dependencies


def topological_sort(dependencies: dict[str, Set[str]]) -> list[str]:
    """
    Perform topological sort on image dependencies.

    Returns images in build order (dependencies built before dependents).

    Args:
        dependencies: Dictionary mapping image name to set of dependencies

    Returns:
        List of image names in topological order (build order)

    Raises:
        CyclicDependencyError: If a circular dependency is detected
    """
    try:
        # TopologicalSorter expects a mapping of node -> predecessors (dependencies)
        sorter = TopologicalSorter(dependencies)
        # static_order() returns a tuple in topological order
        result = list(sorter.static_order())
        return result
    except ValueError as e:
        # TopologicalSorter raises ValueError when it detects a cycle
        raise CyclicDependencyError(
            f"Circular dependency detected in image dependencies: {str(e)}"
        ) from e


def sort_images(images: list) -> list:
    """
    Sort images in build order (dependencies built before dependents).

    This is a convenience wrapper that combines extract_dependencies() and
    topological_sort() to provide a high-level interface that takes Image
    objects and returns them sorted in build order.

    Args:
        images: List of Image objects to sort

    Returns:
        List of Image objects sorted in topological order (build order)

    Raises:
        CyclicDependencyError: If a circular dependency is detected
    """
    # Extract dependencies from images
    dependencies = extract_dependencies(images)

    # Perform topological sort to get names in build order
    sorted_names = topological_sort(dependencies)

    # Create a mapping from name to list of images (multiple image.yml can have same name)
    image_map: dict[str, list] = {}
    for image in images:
        if image.name not in image_map:
            image_map[image.name] = []
        image_map[image.name].append(image)

    # Map sorted names back to Image objects (include all images with same name)
    sorted_images = []
    for name in sorted_names:
        if name in image_map:
            sorted_images.extend(image_map[name])

    return sorted_images
