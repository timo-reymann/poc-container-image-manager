# tests/test_config_integration.py
"""Integration tests for config with building module."""
import pytest
from manager.config import clear_config_cache, get_registry_url
from manager.building import get_registry_addr, get_registry_addr_for_buildkit


class TestBuildingConfigIntegration:
    def setup_method(self):
        clear_config_cache()

    def test_default_registry_urls(self, tmp_path, monkeypatch):
        """Without config, uses localhost:5050."""
        monkeypatch.chdir(tmp_path)

        assert get_registry_url() == "localhost:5050"
        assert get_registry_addr() == "localhost:5050"

    def test_custom_registry_urls(self, tmp_path, monkeypatch):
        """With config, uses custom registry."""
        config_file = tmp_path / ".image-manager.yml"
        config_file.write_text("registry:\n  url: my-registry.com:5000\n")
        monkeypatch.chdir(tmp_path)

        assert get_registry_url() == "my-registry.com:5000"
        assert get_registry_addr() == "my-registry.com:5000"
        # Non-localhost doesn't need host.docker.internal
        assert get_registry_addr_for_buildkit() == "my-registry.com:5000"

    def test_localhost_registry_buildkit_translation(self, tmp_path, monkeypatch):
        """Localhost registry translates to host.docker.internal for buildkit."""
        config_file = tmp_path / ".image-manager.yml"
        config_file.write_text("registry:\n  url: localhost:6000\n")
        monkeypatch.chdir(tmp_path)

        assert get_registry_addr() == "localhost:6000"
        assert get_registry_addr_for_buildkit() == "host.docker.internal:6000"
