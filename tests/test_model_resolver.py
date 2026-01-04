from pathlib import Path
from manager.models import ModelResolver
from manager.config import ImageConfig, TagConfig, VariantConfig, ConfigLoader


def test_resolve_simple_image(tmp_path):
    """Test resolving image with just base tags"""
    # Setup templates
    image_dir = tmp_path / "images" / "python" / "3"
    image_dir.mkdir(parents=True)
    templates_dir = tmp_path / "images" / "python" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "Dockerfile.jinja2").touch()

    config = ImageConfig(
        name="python",
        versions={"uv": "0.8.22"},
        variables={"ENV": "production"},
        tags=[
            TagConfig(
                name="3.13.7",
                versions={"python": "3.13.7"},
                variables={}
            )
        ]
    )

    resolver = ModelResolver()
    image = resolver.resolve(config, image_dir)

    assert image.name == "python"
    assert image.path == image_dir
    assert image.versions == {"uv": "0.8.22"}
    assert len(image.tags) == 1
    assert image.tags[0].name == "3.13.7"
    assert image.tags[0].versions == {"uv": "0.8.22", "python": "3.13.7"}


def test_resolve_image_with_variants(tmp_path):
    """Test resolving image with variants"""
    # Setup templates
    image_dir = tmp_path / "images" / "python" / "3"
    image_dir.mkdir(parents=True)
    templates_dir = tmp_path / "images" / "python" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "Dockerfile.jinja2").touch()
    (templates_dir / "Dockerfile.browser.jinja2").touch()

    config = ImageConfig(
        name="python",
        tags=[
            TagConfig(name="3.13.7", versions={}, variables={}),
            TagConfig(name="3.13.6", versions={}, variables={})
        ],
        variants=[
            VariantConfig(
                name="browser",
                tag_suffix="-browser",
                versions={"chromium": "120.0"},
                variables={}
            )
        ]
    )

    resolver = ModelResolver()
    image = resolver.resolve(config, image_dir)

    assert len(image.variants) == 1
    assert image.variants[0].name == "browser"
    assert len(image.variants[0].tags) == 2
    assert image.variants[0].tags[0].name == "3.13.7-browser"
    assert image.variants[0].tags[1].name == "3.13.6-browser"


def test_resolve_finds_templates_dir(tmp_path):
    """Test resolver finds templates in parent directory"""
    # Setup: version at images/python/3/, templates at images/python/templates/
    image_dir = tmp_path / "images" / "python" / "3"
    image_dir.mkdir(parents=True)
    templates_dir = tmp_path / "images" / "python" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "Dockerfile.jinja2").touch()

    config = ImageConfig(
        name="python",
        tags=[TagConfig(name="3.13.7", versions={}, variables={})]
    )

    resolver = ModelResolver()
    image = resolver.resolve(config, image_dir)

    assert image.template_path == templates_dir / "Dockerfile.jinja2"


def test_name_detection_regular_image():
    """Test that regular images use parent directory name"""
    fixtures_dir = Path(__file__).parent / "fixtures"
    config_file = fixtures_dir / "python" / "3" / "image.yml"

    # Temporarily remove name from config to test auto-detection
    config = ConfigLoader.load(config_file)
    config.name = None  # Simulate missing name

    resolver = ModelResolver()
    image = resolver.resolve(config, config_file.parent)

    # Should use parent directory name "python", not "3"
    assert image.name == "python"


def test_name_detection_base_image_with_flag():
    """Test that base images with flag use directory name"""
    fixtures_dir = Path(__file__).parent / "fixtures"
    config_file = fixtures_dir / "base" / "ubuntu" / "image.yml"

    config = ConfigLoader.load(config_file)
    config.name = None  # Simulate missing name

    resolver = ModelResolver()
    image = resolver.resolve(config, config_file.parent)

    # Should use directory name "ubuntu", not parent "base"
    assert image.name == "ubuntu"


def test_name_detection_base_image_by_path():
    """Test that images under base/ are detected as base images"""
    fixtures_dir = Path(__file__).parent / "fixtures"
    # Create a test path that contains "base"
    test_path = fixtures_dir / "base" / "alpine"

    # Mock config without is_base_image flag
    config = ImageConfig(
        name=None,
        is_base_image=False,  # No flag set
        tags=[TagConfig(name="3.18", versions={}, variables={})],
        versions={},
        variables={},
        variants=[]
    )

    # Create minimal directory structure for this test
    test_path.mkdir(parents=True, exist_ok=True)
    templates_dir = fixtures_dir / "base" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "Dockerfile.jinja2").touch()

    resolver = ModelResolver()
    # Path contains "base", so should use directory name
    image = resolver.resolve(config, test_path)

    # Should use directory name "alpine" for base images
    assert image.name == "alpine"


def test_explicit_name_overrides_detection():
    """Test that explicit config.name always wins"""
    fixtures_dir = Path(__file__).parent / "fixtures"
    config_file = fixtures_dir / "python" / "3" / "image.yml"

    config = ConfigLoader.load(config_file)
    config.name = "custom-python"  # Explicit override

    resolver = ModelResolver()
    image = resolver.resolve(config, config_file.parent)

    assert image.name == "custom-python"
