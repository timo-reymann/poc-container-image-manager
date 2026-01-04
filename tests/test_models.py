from pathlib import Path
from manager.models import Image, Tag, Variant


def test_tag_with_merged_data():
    """Test Tag stores merged versions and variables"""
    tag = Tag(
        name="3.13.7",
        versions={"python": "3.13.7", "uv": "0.8.22"},
        variables={"ENV": "production", "DEBUG": "false"}
    )

    assert tag.name == "3.13.7"
    assert tag.versions["python"] == "3.13.7"
    assert tag.variables["ENV"] == "production"


def test_variant_with_generated_tags():
    """Test Variant stores generated tags"""
    tag1 = Tag(name="3.13.7-browser", versions={}, variables={})
    tag2 = Tag(name="3.13.6-browser", versions={}, variables={})

    variant = Variant(
        name="browser",
        template_path=Path("/fake/Dockerfile.browser.tmpl"),
        tags=[tag1, tag2]
    )

    assert variant.name == "browser"
    assert len(variant.tags) == 2
    assert variant.tags[0].name == "3.13.7-browser"
    assert variant.template_path == Path("/fake/Dockerfile.browser.tmpl")


def test_image_with_tags_and_variants():
    """Test Image stores resolved data"""
    tag = Tag(name="3.13.7", versions={}, variables={})
    variant = Variant(
        name="browser",
        template_path=Path("/fake/Dockerfile.tmpl"),
        tags=[]
    )

    image = Image(
        name="python",
        path=Path("/fake/images/python/3"),
        template_path=Path("/fake/Dockerfile.tmpl"),
        versions={"uv": "0.8.22"},
        variables={"ENV": "production"},
        tags=[tag],
        variants=[variant],
        is_base_image=False,
        extends=None,
        aliases={}
    )

    assert image.name == "python"
    assert len(image.tags) == 1
    assert len(image.variants) == 1
    assert image.versions["uv"] == "0.8.22"


def test_variant_with_aliases():
    """Test Variant stores aliases dict"""
    tag1 = Tag(name="9.0.100-browser", versions={}, variables={})

    variant = Variant(
        name="browser",
        template_path=Path("/fake/Dockerfile.browser.jinja2"),
        tags=[tag1],
        aliases={"9-browser": "9.0.100-browser"}
    )

    assert variant.aliases == {"9-browser": "9.0.100-browser"}


def test_tag_has_rootfs_fields():
    """Test Tag dataclass has rootfs_user and rootfs_copy"""
    tag = Tag(
        name="1.0",
        versions={},
        variables={},
        rootfs_user="1000:1000",
        rootfs_copy=True
    )
    assert tag.rootfs_user == "1000:1000"
    assert tag.rootfs_copy is True


def test_tag_rootfs_defaults():
    """Test Tag dataclass has correct defaults for rootfs fields"""
    tag = Tag(name="1.0", versions={}, variables={})
    assert tag.rootfs_user == "0:0"
    assert tag.rootfs_copy is True


def test_variant_has_rootfs_fields():
    """Test Variant dataclass has rootfs_user and rootfs_copy"""
    variant = Variant(
        name="browser",
        template_path=Path("test.jinja2"),
        tags=[],
        rootfs_user="0:0",
        rootfs_copy=False
    )
    assert variant.rootfs_user == "0:0"
    assert variant.rootfs_copy is False


def test_image_has_rootfs_fields():
    """Test Image dataclass has rootfs_user and rootfs_copy"""
    image = Image(
        name="test",
        path=Path("test"),
        template_path=Path("test.jinja2"),
        versions={},
        variables={},
        tags=[],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={},
        rootfs_user="1000:1000",
        rootfs_copy=False
    )
    assert image.rootfs_user == "1000:1000"
    assert image.rootfs_copy is False


def test_resolver_inherits_rootfs_from_image(tmp_path):
    """Test ModelResolver inherits rootfs settings from image to tags"""
    from manager.config import ConfigLoader
    from manager.models import ModelResolver

    config_file = tmp_path / "image.yml"
    config_file.write_text("""
name: test
rootfs_user: "1000:1000"
rootfs_copy: false
tags:
  - name: "1.0"
""")
    (tmp_path / "Dockerfile.jinja2").write_text("FROM base")

    config = ConfigLoader.load(config_file)
    resolver = ModelResolver()
    image = resolver.resolve(config, tmp_path)

    assert image.rootfs_user == "1000:1000"
    assert image.rootfs_copy is False
    assert image.tags[0].rootfs_user == "1000:1000"
    assert image.tags[0].rootfs_copy is False


def test_resolver_tag_overrides_image_rootfs(tmp_path):
    """Test tag-level rootfs settings override image-level"""
    from manager.config import ConfigLoader
    from manager.models import ModelResolver

    config_file = tmp_path / "image.yml"
    config_file.write_text("""
name: test
rootfs_user: "1000:1000"
rootfs_copy: true
tags:
  - name: "1.0"
    rootfs_user: "0:0"
    rootfs_copy: false
""")
    (tmp_path / "Dockerfile.jinja2").write_text("FROM base")

    config = ConfigLoader.load(config_file)
    resolver = ModelResolver()
    image = resolver.resolve(config, tmp_path)

    assert image.tags[0].rootfs_user == "0:0"
    assert image.tags[0].rootfs_copy is False


def test_resolver_variant_inherits_rootfs(tmp_path):
    """Test variant inherits rootfs from image, tag overrides cascade"""
    from manager.config import ConfigLoader
    from manager.models import ModelResolver

    config_file = tmp_path / "image.yml"
    config_file.write_text("""
name: test
rootfs_user: "1000:1000"
tags:
  - name: "1.0"
variants:
  - name: slim
    tag_suffix: "-slim"
    rootfs_user: "0:0"
""")
    (tmp_path / "Dockerfile.jinja2").write_text("FROM base")
    (tmp_path / "Dockerfile.slim.jinja2").write_text("FROM base")

    config = ConfigLoader.load(config_file)
    resolver = ModelResolver()
    image = resolver.resolve(config, tmp_path)

    assert image.variants[0].rootfs_user == "0:0"
    assert image.variants[0].rootfs_copy is True  # Default, not overridden
