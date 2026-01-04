import pytest
from pathlib import Path
from manager.dependency_graph import (
    extract_base_image_refs,
    extract_dependencies,
    topological_sort,
    sort_images,
    CyclicDependencyError
)
from manager.models import Image, Tag


def test_extract_single_base_image_ref():
    """Test extracting a single base image reference from template"""
    template = 'FROM {{ "base" | resolve_base_image }}'
    refs = extract_base_image_refs(template)
    assert refs == {"base"}


def test_extract_multiple_base_image_refs():
    """Test extracting multiple base image references"""
    template = '''
    FROM {{ "base" | resolve_base_image }}
    COPY --from={{ "builder" | resolve_base_image }} /app /app
    '''
    refs = extract_base_image_refs(template)
    assert refs == {"base", "builder"}


def test_extract_no_base_image_refs():
    """Test template with no base image references"""
    template = 'FROM ubuntu:22.04'
    refs = extract_base_image_refs(template)
    assert refs == set()


def test_extract_with_whitespace_variations():
    """Test extraction handles whitespace variations"""
    template = '{{  "base"  |  resolve_base_image  }}'
    refs = extract_base_image_refs(template)
    assert refs == {"base"}


def test_extract_with_single_quotes():
    """Test extraction works with single quotes"""
    template = "{{ 'base' | resolve_base_image }}"
    refs = extract_base_image_refs(template)
    assert refs == {"base"}


def test_extract_dependencies_single_image(tmp_path):
    """Test extracting dependencies from a single image with one dependency"""
    # Create template file
    template_file = tmp_path / "Dockerfile.jinja2"
    template_file.write_text('FROM {{ "ubuntu" | resolve_base_image }}')

    image = Image(
        name="app",
        path=tmp_path,
        template_path=template_file,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    deps = extract_dependencies([image])
    assert "app" in deps
    assert "ubuntu" in deps["app"]


def test_extract_dependencies_multiple_images(tmp_path):
    """Test extracting dependencies from multiple images"""
    # Create template files
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_template = base_dir / "Dockerfile.jinja2"
    base_template.write_text('FROM ubuntu:22.04')

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_template = app_dir / "Dockerfile.jinja2"
    app_template.write_text('FROM {{ "base" | resolve_base_image }}')

    image1 = Image(
        name="base",
        path=base_dir,
        template_path=base_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=True,
        extends=None,
        aliases={}
    )

    image2 = Image(
        name="app",
        path=app_dir,
        template_path=app_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    deps = extract_dependencies([image1, image2])
    assert "base" in deps
    assert "app" in deps
    assert len(deps) == 2
    assert len(deps["base"]) == 0  # base has no internal dependencies
    assert "base" in deps["app"]  # app depends on base


def test_extract_dependencies_no_dependencies(tmp_path):
    """Test image with no dependencies (external base image)"""
    # Create template file with external base image (not using resolve_base_image)
    template_file = tmp_path / "Dockerfile.jinja2"
    template_file.write_text('FROM ubuntu:22.04')

    image = Image(
        name="standalone",
        path=tmp_path,
        template_path=template_file,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    deps = extract_dependencies([image])
    assert "standalone" in deps
    assert len(deps["standalone"]) == 0


def test_extract_dependencies_transitive(tmp_path):
    """Test extracting transitive dependencies (A -> B -> C)"""
    # Create template files with dependency chain: app -> middle -> base
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_template = base_dir / "Dockerfile.jinja2"
    base_template.write_text('FROM ubuntu:22.04')

    middle_dir = tmp_path / "middle"
    middle_dir.mkdir()
    middle_template = middle_dir / "Dockerfile.jinja2"
    middle_template.write_text('FROM {{ "base" | resolve_base_image }}')

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_template = app_dir / "Dockerfile.jinja2"
    app_template.write_text('FROM {{ "middle" | resolve_base_image }}')

    base = Image(
        name="base",
        path=base_dir,
        template_path=base_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=True,
        extends=None,
        aliases={}
    )

    middle = Image(
        name="middle",
        path=middle_dir,
        template_path=middle_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    app = Image(
        name="app",
        path=app_dir,
        template_path=app_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    deps = extract_dependencies([base, middle, app])
    assert "base" in deps
    assert "middle" in deps
    assert "app" in deps
    assert len(deps["base"]) == 0  # base has no dependencies
    assert "base" in deps["middle"]  # middle depends on base
    assert "middle" in deps["app"]  # app depends on middle


def test_extract_dependencies_with_variant(tmp_path):
    """Test that variant templates are parsed for dependencies"""
    # Create main template
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    main_template = app_dir / "Dockerfile.jinja2"
    main_template.write_text('FROM {{ "base" | resolve_base_image }}')

    # Create variant template with different dependency
    variant_template = app_dir / "Dockerfile.semantic.jinja2"
    variant_template.write_text('FROM {{ "builder" | resolve_base_image }}')

    from manager.models import Variant

    variant = Variant(
        name="semantic",
        template_path=variant_template,
        tags=[Tag(name="latest-semantic", versions={}, variables={})],
        aliases={}
    )

    image = Image(
        name="app",
        path=app_dir,
        template_path=main_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[variant],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    deps = extract_dependencies([image])
    assert "app" in deps
    # Should contain dependencies from both main template and variant template
    assert "base" in deps["app"]
    assert "builder" in deps["app"]
    assert len(deps["app"]) == 2


def test_extract_dependencies_with_extends(tmp_path):
    """Test that extends field is included in dependencies"""
    # Create template without resolve_base_image reference
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    template = app_dir / "Dockerfile.jinja2"
    template.write_text("FROM ubuntu:22.04")

    image = Image(
        name="custom-python",
        path=app_dir,
        template_path=template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends="python",
        aliases={}
    )

    deps = extract_dependencies([image])
    assert "custom-python" in deps
    # Should contain the extends dependency
    assert "python" in deps["custom-python"]
    assert len(deps["custom-python"]) == 1


def test_topological_sort_simple_chain():
    """Test topological sort with simple chain: A -> B -> C"""
    dependencies = {
        "base": set(),
        "middle": {"base"},
        "app": {"middle"}
    }

    result = topological_sort(dependencies)

    # Verify result is a list
    assert isinstance(result, list)
    assert len(result) == 3

    # Verify base comes before middle, and middle comes before app
    base_idx = result.index("base")
    middle_idx = result.index("middle")
    app_idx = result.index("app")

    assert base_idx < middle_idx
    assert middle_idx < app_idx


def test_topological_sort_independent_images():
    """Test topological sort with independent images (no dependencies)"""
    dependencies = {
        "image1": set(),
        "image2": set(),
        "image3": set()
    }

    result = topological_sort(dependencies)

    # All images should be in the result
    assert len(result) == 3
    assert set(result) == {"image1", "image2", "image3"}


def test_topological_sort_cyclic_dependency():
    """Test that cyclic dependencies raise CyclicDependencyError"""
    dependencies = {
        "a": {"b"},
        "b": {"c"},
        "c": {"a"}  # Creates a cycle: a -> b -> c -> a
    }

    with pytest.raises(CyclicDependencyError) as exc_info:
        topological_sort(dependencies)

    # Verify the error message contains useful information
    assert "cycle" in str(exc_info.value).lower()


def test_topological_sort_diamond_dependency():
    """Test topological sort with diamond dependency: A -> B, A -> C, B -> D, C -> D"""
    dependencies = {
        "base": set(),
        "b": {"base"},
        "c": {"base"},
        "app": {"b", "c"}
    }

    result = topological_sort(dependencies)

    # Verify all images are present
    assert len(result) == 4
    assert set(result) == {"base", "b", "c", "app"}

    # Verify base comes before b and c
    base_idx = result.index("base")
    b_idx = result.index("b")
    c_idx = result.index("c")
    app_idx = result.index("app")

    assert base_idx < b_idx
    assert base_idx < c_idx
    # Both b and c must come before app
    assert b_idx < app_idx
    assert c_idx < app_idx


def test_sort_images_simple_chain(tmp_path):
    """Test sort_images with simple dependency chain: base -> middle -> app"""
    # Create template files
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_template = base_dir / "Dockerfile.jinja2"
    base_template.write_text('FROM ubuntu:22.04')

    middle_dir = tmp_path / "middle"
    middle_dir.mkdir()
    middle_template = middle_dir / "Dockerfile.jinja2"
    middle_template.write_text('FROM {{ "base" | resolve_base_image }}')

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_template = app_dir / "Dockerfile.jinja2"
    app_template.write_text('FROM {{ "middle" | resolve_base_image }}')

    # Create Image objects
    base = Image(
        name="base",
        path=base_dir,
        template_path=base_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=True,
        extends=None,
        aliases={}
    )

    middle = Image(
        name="middle",
        path=middle_dir,
        template_path=middle_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    app = Image(
        name="app",
        path=app_dir,
        template_path=app_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    # Sort images (input order: app, base, middle - intentionally unsorted)
    result = sort_images([app, base, middle])

    # Verify we get Image objects back
    assert all(isinstance(img, Image) for img in result)
    assert len(result) == 3

    # Verify correct build order
    assert result[0].name == "base"
    assert result[1].name == "middle"
    assert result[2].name == "app"


def test_sort_images_independent_images(tmp_path):
    """Test sort_images with independent images (no dependencies)"""
    # Create template files for independent images
    images = []
    for i in range(3):
        img_dir = tmp_path / f"image{i}"
        img_dir.mkdir()
        template = img_dir / "Dockerfile.jinja2"
        template.write_text('FROM ubuntu:22.04')

        images.append(Image(
            name=f"image{i}",
            path=img_dir,
            template_path=template,
            versions={},
            variables={},
            tags=[Tag(name="latest", versions={}, variables={})],
            variants=[],
            is_base_image=False,
            extends=None,
            aliases={}
        ))

    # Sort images
    result = sort_images(images)

    # All images should be returned
    assert len(result) == 3
    result_names = {img.name for img in result}
    assert result_names == {"image0", "image1", "image2"}


def test_sort_images_diamond_dependency(tmp_path):
    """Test sort_images with diamond dependency pattern"""
    # Create template files: app depends on both b and c, which both depend on base
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_template = base_dir / "Dockerfile.jinja2"
    base_template.write_text('FROM ubuntu:22.04')

    b_dir = tmp_path / "b"
    b_dir.mkdir()
    b_template = b_dir / "Dockerfile.jinja2"
    b_template.write_text('FROM {{ "base" | resolve_base_image }}')

    c_dir = tmp_path / "c"
    c_dir.mkdir()
    c_template = c_dir / "Dockerfile.jinja2"
    c_template.write_text('FROM {{ "base" | resolve_base_image }}')

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_template = app_dir / "Dockerfile.jinja2"
    app_template.write_text('''
    FROM {{ "b" | resolve_base_image }}
    COPY --from={{ "c" | resolve_base_image }} /app /app
    ''')

    # Create Image objects
    base = Image(
        name="base",
        path=base_dir,
        template_path=base_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=True,
        extends=None,
        aliases={}
    )

    b = Image(
        name="b",
        path=b_dir,
        template_path=b_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    c = Image(
        name="c",
        path=c_dir,
        template_path=c_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    app = Image(
        name="app",
        path=app_dir,
        template_path=app_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    # Sort images
    result = sort_images([app, c, b, base])

    # Verify all images are present
    assert len(result) == 4
    result_names = [img.name for img in result]

    # Verify ordering constraints
    base_idx = result_names.index("base")
    b_idx = result_names.index("b")
    c_idx = result_names.index("c")
    app_idx = result_names.index("app")

    # base must come before b and c
    assert base_idx < b_idx
    assert base_idx < c_idx
    # Both b and c must come before app
    assert b_idx < app_idx
    assert c_idx < app_idx


def test_sort_images_cyclic_dependency(tmp_path):
    """Test that sort_images raises CyclicDependencyError for circular dependencies"""
    # Create circular dependency: a -> b -> c -> a
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    a_template = a_dir / "Dockerfile.jinja2"
    a_template.write_text('FROM {{ "b" | resolve_base_image }}')

    b_dir = tmp_path / "b"
    b_dir.mkdir()
    b_template = b_dir / "Dockerfile.jinja2"
    b_template.write_text('FROM {{ "c" | resolve_base_image }}')

    c_dir = tmp_path / "c"
    c_dir.mkdir()
    c_template = c_dir / "Dockerfile.jinja2"
    c_template.write_text('FROM {{ "a" | resolve_base_image }}')

    images = [
        Image(
            name="a",
            path=a_dir,
            template_path=a_template,
            versions={},
            variables={},
            tags=[Tag(name="latest", versions={}, variables={})],
            variants=[],
            is_base_image=False,
            extends=None,
            aliases={}
        ),
        Image(
            name="b",
            path=b_dir,
            template_path=b_template,
            versions={},
            variables={},
            tags=[Tag(name="latest", versions={}, variables={})],
            variants=[],
            is_base_image=False,
            extends=None,
            aliases={}
        ),
        Image(
            name="c",
            path=c_dir,
            template_path=c_template,
            versions={},
            variables={},
            tags=[Tag(name="latest", versions={}, variables={})],
            variants=[],
            is_base_image=False,
            extends=None,
            aliases={}
        )
    ]

    # Should raise CyclicDependencyError
    with pytest.raises(CyclicDependencyError) as exc_info:
        sort_images(images)

    # Verify error message contains useful information
    assert "cycle" in str(exc_info.value).lower()


def test_sort_images_empty_list():
    """Test sort_images with empty list"""
    result = sort_images([])
    assert result == []


def test_sort_images_with_missing_dependency(tmp_path):
    """
    Test that missing dependencies are acceptable during sorting.

    Missing dependencies (base images not in the managed set) are handled
    gracefully during the sort phase. The actual error is caught later at
    render time when resolve_base_image is called, which provides better
    context and error messages.

    This documents the intended behavior: sorting only considers dependencies
    between managed images, and external/missing references are caught at
    render time by the Jinja2 filter.
    """
    # Create an image that references a non-existent base image
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_template = app_dir / "Dockerfile.jinja2"
    # Reference "nonexistent" which is not in our managed images
    app_template.write_text('FROM {{ "nonexistent" | resolve_base_image }}')

    app = Image(
        name="app",
        path=app_dir,
        template_path=app_template,
        versions={},
        variables={},
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    # Sorting should succeed - missing dependencies are ignored during sort
    result = sort_images([app])

    # The image should be in the result
    assert len(result) == 1
    assert result[0].name == "app"

    # Note: The actual error will be raised when rendering the Dockerfile
    # via the resolve_base_image Jinja2 filter in rendering.py
