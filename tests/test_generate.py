"""Tests for the generate command with rootfs integration."""

from pathlib import Path


def test_generate_merges_rootfs(tmp_path, monkeypatch):
    """Test that generate command merges rootfs into dist"""
    # Setup image structure
    images_dir = tmp_path / "images" / "test"
    version_dir = images_dir / "1"
    version_dir.mkdir(parents=True)

    # Image-level rootfs
    (images_dir / "rootfs" / "etc").mkdir(parents=True)
    (images_dir / "rootfs" / "etc" / "image.conf").write_text("image-level")

    # Version-level rootfs (override)
    (version_dir / "rootfs" / "etc").mkdir(parents=True)
    (version_dir / "rootfs" / "etc" / "image.conf").write_text("version-level")
    (version_dir / "rootfs" / "etc" / "version.conf").write_text("only-version")

    # Config and template
    (version_dir / "image.yml").write_text("""
name: test
tags:
  - name: "1.0"
""")
    (version_dir / "Dockerfile.jinja2").write_text("FROM base:1.0\nRUN echo hello")
    (version_dir / "test.yml.jinja2").write_text("schemaVersion: '2.0.0'")

    # Create dist dir
    (tmp_path / "dist").mkdir()

    monkeypatch.chdir(tmp_path)

    # Run generate
    from manager.__main__ import cmd_generate
    result = cmd_generate([])

    assert result == 0

    # Check merged rootfs
    merged = tmp_path / "dist" / "test" / "1.0" / "rootfs"
    assert merged.exists()
    assert (merged / "etc" / "image.conf").read_text() == "version-level"  # Later wins
    assert (merged / "etc" / "version.conf").read_text() == "only-version"


def test_generate_injects_copy_in_dockerfile(tmp_path, monkeypatch):
    """Test that generate injects COPY when rootfs exists"""
    images_dir = tmp_path / "images" / "test" / "1"
    images_dir.mkdir(parents=True)

    (images_dir / "rootfs" / "etc").mkdir(parents=True)
    (images_dir / "rootfs" / "etc" / "config").write_text("content")

    (images_dir / "image.yml").write_text("""
name: test
rootfs_user: "1000:1000"
tags:
  - name: "1.0"
""")
    (images_dir / "Dockerfile.jinja2").write_text("FROM base:1.0\nRUN echo hello")
    (images_dir / "test.yml.jinja2").write_text("schemaVersion: '2.0.0'")

    (tmp_path / "dist").mkdir()
    monkeypatch.chdir(tmp_path)

    from manager.__main__ import cmd_generate
    cmd_generate([])

    dockerfile = (tmp_path / "dist" / "test" / "1.0" / "Dockerfile").read_text()
    assert "COPY --chown=1000:1000 rootfs/ /" in dockerfile


def test_generate_no_rootfs_no_copy(tmp_path, monkeypatch):
    """Test that generate doesn't inject COPY when no rootfs"""
    images_dir = tmp_path / "images" / "test" / "1"
    images_dir.mkdir(parents=True)

    (images_dir / "image.yml").write_text("""
name: test
tags:
  - name: "1.0"
""")
    (images_dir / "Dockerfile.jinja2").write_text("FROM base:1.0\nRUN echo hello")
    (images_dir / "test.yml.jinja2").write_text("schemaVersion: '2.0.0'")

    (tmp_path / "dist").mkdir()
    monkeypatch.chdir(tmp_path)

    from manager.__main__ import cmd_generate
    cmd_generate([])

    dockerfile = (tmp_path / "dist" / "test" / "1.0" / "Dockerfile").read_text()
    assert "COPY" not in dockerfile
    assert not (tmp_path / "dist" / "test" / "1.0" / "rootfs").exists()


def test_generate_variant_rootfs(tmp_path, monkeypatch):
    """Test that generate handles variant-specific rootfs"""
    images_dir = tmp_path / "images" / "test" / "1"
    images_dir.mkdir(parents=True)

    # Version-level rootfs
    (images_dir / "rootfs" / "etc").mkdir(parents=True)
    (images_dir / "rootfs" / "etc" / "base.conf").write_text("base-config")

    # Variant-specific rootfs
    variant_dir = images_dir / "myvariant"
    variant_dir.mkdir(parents=True)
    (variant_dir / "rootfs" / "etc").mkdir(parents=True)
    (variant_dir / "rootfs" / "etc" / "variant.conf").write_text("variant-config")

    (images_dir / "image.yml").write_text("""
name: test
tags:
  - name: "1.0"
variants:
  - name: myvariant
    template: variant.jinja2
    tag_suffix: "-myvariant"
""")
    (images_dir / "Dockerfile.jinja2").write_text("FROM base:1.0\nRUN echo hello")
    (images_dir / "test.yml.jinja2").write_text("schemaVersion: '2.0.0'")
    (images_dir / "variant.jinja2").write_text("FROM test:1.0\nRUN echo variant")

    (tmp_path / "dist").mkdir()
    monkeypatch.chdir(tmp_path)

    from manager.__main__ import cmd_generate
    cmd_generate([])

    # Check variant merged rootfs (tag name is base tag + suffix: "1.0-myvariant")
    variant_rootfs = tmp_path / "dist" / "test" / "1.0-myvariant" / "rootfs"
    assert variant_rootfs.exists()
    assert (variant_rootfs / "etc" / "base.conf").read_text() == "base-config"
    assert (variant_rootfs / "etc" / "variant.conf").read_text() == "variant-config"
