# tests/test_ci_generator.py
import pytest
from pathlib import Path
from manager.ci_generator import build_ci_context, build_extended_context, generate_custom_ci
from manager.models import Image, Tag, Variant
from manager.config import clear_config_cache


def test_build_ci_context_single_image(tmp_path):
    """Test context generation for single image without dependencies."""
    # Create a template file (required for extract_dependencies)
    template_file = tmp_path / "Dockerfile.jinja2"
    template_file.write_text('FROM ubuntu:22.04')

    image = Image(
        name="base",
        path=tmp_path,
        template_path=template_file,
        versions={},
        variables={},
        tags=[Tag(name="2025.09", versions={}, variables={})],
        variants=[],
        is_base_image=True,
        extends=None,
        aliases={},
    )

    context = build_ci_context([image])

    assert len(context["images"]) == 1
    assert context["images"][0]["name"] == "base"
    assert context["images"][0]["dependencies"] == []
    assert "2025.09" in context["images"][0]["tags"]
    assert context["platforms"] == ["amd64", "arm64"]
    assert "generated_at" in context


def test_build_ci_context_with_dependencies(tmp_path):
    """Test context generation with image dependencies."""
    # Create template files that define dependencies
    base_tpl = tmp_path / "base" / "Dockerfile.jinja2"
    base_tpl.parent.mkdir()
    base_tpl.write_text("FROM ubuntu:22.04")

    python_tpl = tmp_path / "python" / "Dockerfile.jinja2"
    python_tpl.parent.mkdir()
    python_tpl.write_text('FROM {{ "base" | resolve_base_image }}')

    base_image = Image(
        name="base",
        path=tmp_path / "base",
        template_path=base_tpl,
        versions={},
        variables={},
        tags=[Tag(name="2025.09", versions={}, variables={})],
        variants=[],
        is_base_image=True,
        extends=None,
        aliases={},
    )

    python_image = Image(
        name="python",
        path=tmp_path / "python",
        template_path=python_tpl,
        versions={},
        variables={},
        tags=[Tag(name="3.13.7", versions={}, variables={})],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={},
    )

    context = build_ci_context([base_image, python_image])

    assert len(context["images"]) == 2
    # base has no dependencies
    base_ctx = next(i for i in context["images"] if i["name"] == "base")
    assert base_ctx["dependencies"] == []
    # python depends on base
    python_ctx = next(i for i in context["images"] if i["name"] == "python")
    assert python_ctx["dependencies"] == ["base"]


def test_generate_gitlab_ci(tmp_path):
    """Test generating GitLab CI configuration file."""
    from manager.ci_generator import generate_gitlab_ci

    # Create a minimal template file for dependency extraction
    tpl = tmp_path / "src" / "Dockerfile.jinja2"
    tpl.parent.mkdir(parents=True)
    tpl.write_text("FROM ubuntu:22.04")

    image = Image(
        name="base",
        path=tmp_path / "src",
        template_path=tpl,
        tags=[Tag(name="2025.09", versions={}, variables={})],
        variants=[],
        extends=None,
        versions={},
        variables={},
        is_base_image=False,
        aliases={},
    )

    output_path = tmp_path / ".gitlab" / "ci" / "images.yml"
    generate_gitlab_ci([image], output_path)

    assert output_path.exists()
    content = output_path.read_text()
    assert "stages:" in content
    assert "build-base-amd64:" in content
    assert "build-base-arm64:" in content
    assert "manifest-base:" in content
    assert "test-base:" in content


def test_generate_gitlab_ci_with_dependencies(tmp_path):
    """Test that generated CI has correct job dependencies."""
    from manager.ci_generator import generate_gitlab_ci
    import yaml

    # Create template files
    base_tpl = tmp_path / "src" / "base" / "Dockerfile.jinja2"
    base_tpl.parent.mkdir(parents=True)
    base_tpl.write_text("FROM ubuntu:22.04")

    python_tpl = tmp_path / "src" / "python" / "Dockerfile.jinja2"
    python_tpl.parent.mkdir(parents=True)
    python_tpl.write_text('FROM {{ "base" | resolve_base_image }}')

    base_image = Image(
        name="base",
        path=tmp_path / "src" / "base",
        template_path=base_tpl,
        tags=[Tag(name="2025.09", versions={}, variables={})],
        variants=[],
        extends=None,
        versions={},
        variables={},
        is_base_image=True,
        aliases={},
    )

    python_image = Image(
        name="python",
        path=tmp_path / "src" / "python",
        template_path=python_tpl,
        tags=[Tag(name="3.13.7", versions={}, variables={})],
        variants=[],
        extends=None,
        versions={},
        variables={},
        is_base_image=False,
        aliases={},
    )

    output_path = tmp_path / ".gitlab" / "ci" / "images.yml"
    generate_gitlab_ci([base_image, python_image], output_path)

    content = output_path.read_text()
    config = yaml.safe_load(content)

    # Base build jobs have no needs (template doesn't output needs key)
    assert "needs" not in config.get("build-base-amd64", {})
    assert "needs" not in config.get("build-base-arm64", {})

    # Python build jobs need manifest-base
    assert "manifest-base" in config["build-python-amd64"]["needs"]
    assert "manifest-base" in config["build-python-arm64"]["needs"]

    # Manifest jobs need their build jobs
    assert "build-python-amd64" in config["manifest-python"]["needs"]
    assert "build-python-arm64" in config["manifest-python"]["needs"]

    # Test jobs need manifest
    assert "manifest-python" in config["test-python"]["needs"]


def test_build_extended_context(tmp_path):
    """Test extended context includes config values."""
    # Create a template file
    template_file = tmp_path / "Dockerfile.jinja2"
    template_file.write_text('FROM ubuntu:22.04')

    image = Image(
        name="base",
        path=tmp_path,
        template_path=template_file,
        versions={},
        variables={},
        tags=[Tag(name="2025.09", versions={}, variables={})],
        variants=[],
        is_base_image=True,
        extends=None,
        aliases={},
    )

    # Clear config cache to use defaults
    clear_config_cache()

    context = build_extended_context([image])

    # Should have standard context
    assert len(context["images"]) == 1
    assert context["platforms"] == ["amd64", "arm64"]
    assert "generated_at" in context

    # Should have config section
    assert "config" in context
    assert "registry" in context["config"]
    assert "registries" in context["config"]
    assert "cache" in context["config"]
    assert "labels" in context["config"]


def test_generate_custom_ci(tmp_path):
    """Test generating CI from custom template."""
    # Create a custom template directory
    template_dir = tmp_path / "templates"
    template_dir.mkdir()

    # Create main template with extended context usage
    main_template = template_dir / "pipeline.yml.j2"
    main_template.write_text("""# Custom CI for {{ images | length }} images
# Registry: {{ config.registry }}
{% for image in images %}
build-{{ image.name }}:
  image: {{ image.name }}
  tags: {{ image.tags | join(', ') }}
{% endfor %}
""")

    # Create source template file
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    tpl = src_dir / "Dockerfile.jinja2"
    tpl.write_text("FROM ubuntu:22.04")

    image = Image(
        name="myimage",
        path=src_dir,
        template_path=tpl,
        tags=[Tag(name="1.0", versions={}, variables={}), Tag(name="2.0", versions={}, variables={})],
        variants=[],
        extends=None,
        versions={},
        variables={},
        is_base_image=False,
        aliases={},
    )

    # Clear config cache
    clear_config_cache()

    output_path = tmp_path / "output" / "ci.yml"
    generate_custom_ci([image], template_dir, output_path)

    assert output_path.exists()
    content = output_path.read_text()
    assert "# Custom CI for 1 images" in content
    assert "Registry: localhost:5050" in content
    assert "build-myimage:" in content
    assert "1.0, 2.0" in content


def test_generate_custom_ci_with_includes(tmp_path):
    """Test custom template with includes."""
    # Create a custom template directory
    template_dir = tmp_path / "templates"
    template_dir.mkdir()

    # Create an include file
    include_file = template_dir / "job.yml.j2"
    include_file.write_text("""  stage: build
  script: build.sh""")

    # Create main template that uses include
    main_template = template_dir / "pipeline.yml.j2"
    main_template.write_text("""stages:
  - build

{% for image in images %}
build-{{ image.name }}:
{% include 'job.yml.j2' %}
{% endfor %}
""")

    # Create source template file
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    tpl = src_dir / "Dockerfile.jinja2"
    tpl.write_text("FROM ubuntu:22.04")

    image = Image(
        name="test",
        path=src_dir,
        template_path=tpl,
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        extends=None,
        versions={},
        variables={},
        is_base_image=False,
        aliases={},
    )

    clear_config_cache()

    output_path = tmp_path / "ci.yml"
    generate_custom_ci([image], template_dir, output_path)

    assert output_path.exists()
    content = output_path.read_text()
    assert "stages:" in content
    assert "build-test:" in content
    assert "stage: build" in content
    assert "script: build.sh" in content


def test_generate_custom_ci_missing_template_dir(tmp_path):
    """Test error when template directory doesn't exist."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    tpl = src_dir / "Dockerfile.jinja2"
    tpl.write_text("FROM ubuntu:22.04")

    image = Image(
        name="test",
        path=src_dir,
        template_path=tpl,
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        extends=None,
        versions={},
        variables={},
        is_base_image=False,
        aliases={},
    )

    with pytest.raises(FileNotFoundError, match="Template directory not found"):
        generate_custom_ci([image], tmp_path / "nonexistent", tmp_path / "output.yml")


def test_generate_custom_ci_missing_main_template(tmp_path):
    """Test error when main template is missing."""
    # Create template dir without main template
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "other.yml.j2").write_text("some content")

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    tpl = src_dir / "Dockerfile.jinja2"
    tpl.write_text("FROM ubuntu:22.04")

    image = Image(
        name="test",
        path=src_dir,
        template_path=tpl,
        tags=[Tag(name="latest", versions={}, variables={})],
        variants=[],
        extends=None,
        versions={},
        variables={},
        is_base_image=False,
        aliases={},
    )

    with pytest.raises(FileNotFoundError, match="Main template 'pipeline.yml.j2' not found"):
        generate_custom_ci([image], template_dir, tmp_path / "output.yml")
