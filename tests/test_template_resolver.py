from pathlib import Path
from manager.template_resolver import TemplateResolver


def test_resolve_default_template(tmp_path):
    """Test resolves default template when no explicit or variant"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "Dockerfile.jinja2").touch()

    resolver = TemplateResolver()
    path = resolver.resolve(templates_dir, explicit=None, variant_name=None)

    assert path == templates_dir / "Dockerfile.jinja2"


def test_resolve_explicit_template(tmp_path):
    """Test explicit template takes priority"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "custom.jinja2").touch()
    (templates_dir / "Dockerfile.jinja2").touch()

    resolver = TemplateResolver()
    path = resolver.resolve(templates_dir, explicit="custom.jinja2", variant_name=None)

    assert path == templates_dir / "custom.jinja2"


def test_resolve_variant_template(tmp_path):
    """Test variant-specific template discovered"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "Dockerfile.browser.jinja2").touch()
    (templates_dir / "Dockerfile.jinja2").touch()

    resolver = TemplateResolver()
    path = resolver.resolve(templates_dir, explicit=None, variant_name="browser")

    assert path == templates_dir / "Dockerfile.browser.jinja2"


def test_resolve_variant_fallback_to_default(tmp_path):
    """Test variant falls back to default if specific not found"""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "Dockerfile.jinja2").touch()

    resolver = TemplateResolver()
    path = resolver.resolve(templates_dir, explicit=None, variant_name="browser")

    assert path == templates_dir / "Dockerfile.jinja2"


def test_resolve_raises_if_template_not_found(tmp_path):
    """Test raises error if no template found"""
    import pytest

    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    resolver = TemplateResolver()
    with pytest.raises(FileNotFoundError, match="Template not found"):
        resolver.resolve(templates_dir, explicit=None, variant_name=None)
