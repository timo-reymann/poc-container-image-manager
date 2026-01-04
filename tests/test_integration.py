from pathlib import Path
from manager.config import ConfigLoader
from manager.models import ModelResolver


def test_end_to_end_config_to_model():
    """Test complete flow from YAML to resolved model"""
    fixtures_dir = Path(__file__).parent / "fixtures"
    config_file = fixtures_dir / "python" / "3" / "image.yml"

    # Load config
    config = ConfigLoader.load(config_file)

    # Resolve model
    resolver = ModelResolver()
    image = resolver.resolve(config, config_file.parent)

    # Verify base image data
    assert image.name == "python"
    assert image.versions == {"uv": "0.8.22"}
    assert image.variables == {"ENV": "production"}

    # Verify base tags
    assert len(image.tags) == 2
    assert image.tags[0].name == "3.13.7"
    assert image.tags[0].versions == {"uv": "0.8.22", "python": "3.13.7"}
    assert image.tags[1].name == "3.13.6"

    # Verify variant
    assert len(image.variants) == 1
    browser = image.variants[0]
    assert browser.name == "browser"
    assert len(browser.tags) == 2
    assert browser.tags[0].name == "3.13.7-browser"
    assert browser.tags[0].versions["chromium"] == "120.0"

    # Verify template paths resolved
    assert image.template_path.name == "Dockerfile.jinja2"
    assert browser.template_path.name == "Dockerfile.browser.jinja2"


def test_automatic_alias_generation():
    """Test that aliases are automatically generated from tags"""
    fixtures_dir = Path(__file__).parent / "fixtures"
    config_file = fixtures_dir / "python" / "3" / "image.yml"

    # Load config
    config = ConfigLoader.load(config_file)

    # Resolve model
    resolver = ModelResolver()
    image = resolver.resolve(config, config_file.parent)

    # Verify base aliases auto-generated
    assert "3" in image.aliases
    assert "3.13" in image.aliases

    # Verify aliases point to highest versions
    assert image.aliases["3.13"] == "3.13.7"  # Highest 3.13.x

    # Verify variant has aliases
    if image.variants:
        browser = image.variants[0]
        assert "3-browser" in browser.aliases
        assert "3.13-browser" in browser.aliases


def test_smart_name_detection_integration():
    """Test that smart name detection works end-to-end"""
    fixtures_dir = Path(__file__).parent / "fixtures"

    # Test 1: Regular image (python/3) should get "python"
    python_config_file = fixtures_dir / "python" / "3" / "image.yml"
    python_config = ConfigLoader.load(python_config_file)

    # Remove explicit name to test auto-detection
    original_name = python_config.name
    python_config.name = None

    resolver = ModelResolver()
    python_image = resolver.resolve(python_config, python_config_file.parent)

    assert python_image.name == "python", \
        f"Expected 'python', got '{python_image.name}'"

    # Test 2: Base image should get directory name
    base_config_file = fixtures_dir / "base" / "ubuntu" / "image.yml"
    base_config = ConfigLoader.load(base_config_file)
    base_config.name = None

    base_image = resolver.resolve(base_config, base_config_file.parent)

    assert base_image.name == "ubuntu", \
        f"Expected 'ubuntu', got '{base_image.name}'"

    # Test 3: Explicit name still works
    python_config.name = original_name
    python_image_explicit = resolver.resolve(python_config, python_config_file.parent)
    assert python_image_explicit.name == original_name


def test_images_generated_in_dependency_order():
    """Test that images are generated in dependency order with dependency graph sorting"""
    from manager.dependency_graph import sort_images

    # Load all images from fixtures
    fixtures_dir = Path(__file__).parent / "fixtures"
    resolver = ModelResolver()
    all_images = []

    for image_yaml in fixtures_dir.glob("**/image.yml"):
        config = ConfigLoader.load(image_yaml)
        image = resolver.resolve(config, image_yaml.parent)
        all_images.append(image)

    # Sort images by dependencies
    sorted_images = sort_images(all_images)

    # Verify we got all images back
    assert len(sorted_images) == len(all_images)

    # Verify images are sorted (base images should come first)
    image_names = [img.name for img in sorted_images]

    # Find base images (images with no internal dependencies)
    from manager.dependency_graph import extract_dependencies
    deps = extract_dependencies(sorted_images)

    # Base images should be at the beginning
    # For each image, all its dependencies should appear before it in the sorted list
    for i, image in enumerate(sorted_images):
        image_deps = deps[image.name]
        for dep in image_deps:
            if dep in image_names:  # Only check internal dependencies
                dep_index = image_names.index(dep)
                assert dep_index < i, \
                    f"Dependency '{dep}' of '{image.name}' should come before it in build order"
