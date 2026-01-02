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
