# tests/test_ci_generator.py
import pytest
from pathlib import Path
from manager.ci_generator import build_ci_context
from manager.models import Image, Tag, Variant


def test_build_ci_context_single_image(tmp_path):
    """Test context generation for single image without dependencies."""
    # Create a template file (required for extract_dependencies)
    template_file = tmp_path / "Dockerfile.tpl"
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
    base_tpl = tmp_path / "base" / "Dockerfile.tpl"
    base_tpl.parent.mkdir()
    base_tpl.write_text("FROM ubuntu:22.04")

    python_tpl = tmp_path / "python" / "Dockerfile.tpl"
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
