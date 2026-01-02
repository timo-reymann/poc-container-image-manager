from pathlib import Path
from manager.rootfs import collect_rootfs_paths, merge_rootfs, has_rootfs_content


def test_collect_rootfs_paths_image_level(tmp_path):
    """Test collecting rootfs from image level only"""
    # Setup: images/python/rootfs/etc/config
    image_path = tmp_path / "images" / "python"
    image_path.mkdir(parents=True)
    rootfs = image_path / "rootfs" / "etc"
    rootfs.mkdir(parents=True)
    (rootfs / "config").write_text("image-level")

    version_path = image_path / "3"
    version_path.mkdir()

    paths = collect_rootfs_paths(image_path, version_path, variant_name=None)
    assert len(paths) == 1
    assert paths[0] == image_path / "rootfs"


def test_collect_rootfs_paths_all_levels(tmp_path):
    """Test collecting rootfs from all three levels"""
    image_path = tmp_path / "images" / "python"
    version_path = image_path / "3"
    variant_path = version_path / "semantic-release"

    # Create all levels
    (image_path / "rootfs").mkdir(parents=True)
    (version_path / "rootfs").mkdir(parents=True)
    (variant_path / "rootfs").mkdir(parents=True)

    paths = collect_rootfs_paths(image_path, version_path, variant_name="semantic-release")
    assert len(paths) == 3
    assert paths[0] == image_path / "rootfs"
    assert paths[1] == version_path / "rootfs"
    assert paths[2] == variant_path / "rootfs"


def test_merge_rootfs_later_wins(tmp_path):
    """Test that later levels override earlier ones"""
    # Source directories
    level1 = tmp_path / "level1" / "rootfs"
    level2 = tmp_path / "level2" / "rootfs"
    dest = tmp_path / "merged"

    (level1 / "etc").mkdir(parents=True)
    (level1 / "etc" / "config").write_text("level1")
    (level1 / "etc" / "only-in-level1").write_text("level1-only")

    (level2 / "etc").mkdir(parents=True)
    (level2 / "etc" / "config").write_text("level2")  # Override

    merge_rootfs([level1, level2], dest)

    assert (dest / "etc" / "config").read_text() == "level2"
    assert (dest / "etc" / "only-in-level1").read_text() == "level1-only"


def test_merge_rootfs_preserves_symlinks(tmp_path):
    """Test that symlinks are preserved during merge"""
    level1 = tmp_path / "level1" / "rootfs"
    dest = tmp_path / "merged"

    (level1 / "etc").mkdir(parents=True)
    (level1 / "etc" / "real-file").write_text("content")
    (level1 / "etc" / "link").symlink_to("real-file")

    merge_rootfs([level1], dest)

    assert (dest / "etc" / "link").is_symlink()
    assert (dest / "etc" / "link").read_text() == "content"


def test_has_rootfs_content_empty_dirs(tmp_path):
    """Test that empty directories don't count as content"""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / "empty-dir").mkdir()

    assert has_rootfs_content([rootfs]) is False


def test_has_rootfs_content_with_files(tmp_path):
    """Test that directories with files count as content"""
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "etc" / "config").write_text("content")

    assert has_rootfs_content([rootfs]) is True


def test_has_rootfs_content_hidden_files(tmp_path):
    """Test that hidden files count as content"""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / ".bashrc").write_text("content")

    assert has_rootfs_content([rootfs]) is True
