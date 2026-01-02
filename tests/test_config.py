import os
from pathlib import Path
from unittest.mock import patch
import pytest
from manager.config import ImageConfig, TagConfig, VariantConfig, ConfigLoader, expand_env_vars


class TestExpandEnvVars:
    def test_no_env_vars(self):
        """Literal string returned unchanged."""
        assert expand_env_vars("my-registry.com:5000") == "my-registry.com:5000"

    def test_single_env_var(self):
        """Single ${VAR} is expanded."""
        with patch.dict(os.environ, {"REGISTRY_URL": "prod.example.com:5000"}):
            assert expand_env_vars("${REGISTRY_URL}") == "prod.example.com:5000"

    def test_missing_env_var_returns_none(self):
        """Missing env var returns None."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MISSING_VAR", None)
            assert expand_env_vars("${MISSING_VAR}") is None

    def test_empty_string(self):
        """Empty string returned unchanged."""
        assert expand_env_vars("") == ""

    def test_none_input(self):
        """None input returns None."""
        assert expand_env_vars(None) is None


def test_load_minimal_config(tmp_path):
    """Test loading a minimal valid config"""
    config_file = tmp_path / "image.yml"
    config_file.write_text("""
name: test-image
tags:
  - name: "1.0"
""")

    config = ConfigLoader.load(config_file)
    assert config.name == "test-image"
    assert len(config.tags) == 1
    assert config.tags[0].name == "1.0"


def test_load_config_with_variants(tmp_path):
    """Test loading config with variants"""
    config_file = tmp_path / "image.yml"
    config_file.write_text("""
name: python
versions:
  uv: "0.8.22"
variables:
  ENV: "production"
tags:
  - name: "3.13.7"
    versions:
      python: "3.13.7"
variants:
  - name: browser
    tag_suffix: "-browser"
    versions:
      chromium: "120.0"
""")

    config = ConfigLoader.load(config_file)
    assert config.name == "python"
    assert config.versions == {"uv": "0.8.22"}
    assert config.variables == {"ENV": "production"}
    assert len(config.variants) == 1
    assert config.variants[0].name == "browser"
    assert config.variants[0].tag_suffix == "-browser"


def test_tag_config_defaults():
    """Test TagConfig has sensible defaults"""
    tag = TagConfig(name="1.0")
    assert tag.name == "1.0"
    assert tag.versions == {}
    assert tag.variables == {}


def test_variant_config_requires_suffix():
    """Test VariantConfig requires tag_suffix"""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VariantConfig(name="browser")  # Missing tag_suffix


def test_rootfs_fields_on_tag_config():
    """Test TagConfig has rootfs_user and rootfs_copy fields"""
    tag = TagConfig(name="1.0", rootfs_user="1000:1000", rootfs_copy=False)
    assert tag.rootfs_user == "1000:1000"
    assert tag.rootfs_copy is False


def test_rootfs_fields_defaults():
    """Test rootfs fields have correct defaults"""
    tag = TagConfig(name="1.0")
    assert tag.rootfs_user is None
    assert tag.rootfs_copy is None


def test_variant_rootfs_fields():
    """Test VariantConfig has rootfs_user and rootfs_copy fields"""
    variant = VariantConfig(name="browser", tag_suffix="-browser", rootfs_user="0:0", rootfs_copy=True)
    assert variant.rootfs_user == "0:0"
    assert variant.rootfs_copy is True


def test_image_config_rootfs_fields(tmp_path):
    """Test ImageConfig has rootfs_user and rootfs_copy fields"""
    config_file = tmp_path / "image.yml"
    config_file.write_text("""
name: test
rootfs_user: "1000:1000"
rootfs_copy: false
tags:
  - name: "1.0"
    rootfs_user: "0:0"
    rootfs_copy: true
variants:
  - name: slim
    tag_suffix: "-slim"
    rootfs_user: "1000:1000"
""")
    config = ConfigLoader.load(config_file)
    assert config.rootfs_user == "1000:1000"
    assert config.rootfs_copy is False
    assert config.tags[0].rootfs_user == "0:0"
    assert config.tags[0].rootfs_copy is True
    assert config.variants[0].rootfs_user == "1000:1000"
