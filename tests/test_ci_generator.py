# tests/test_ci_generator.py
import pytest
from pathlib import Path
from manager.ci_generator import build_ci_context
from manager.models import Image, Tag, Variant


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
