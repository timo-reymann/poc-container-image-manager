# rootfs Support Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add rootfs support to copy files into images at build time from a layered directory structure.

**Architecture:** Files merge from three levels (image → version → variant) using "later wins" strategy. COPY instruction is auto-injected after first FROM unless disabled or already present.

**Tech Stack:** Python, Pydantic, Jinja2, shutil, pytest

---

## Task 1: Add rootfs fields to config models

**Files:**
- Modify: `manager/config.py:6-32`
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_rootfs_fields_on_tag_config -v`
Expected: FAIL with validation error or AttributeError

**Step 3: Write implementation**

Edit `manager/config.py`:

```python
class TagConfig(BaseModel):
    """Configuration for a single tag"""
    name: str
    versions: dict[str, str] = {}
    variables: dict[str, str] = {}
    rootfs_user: str | None = None
    rootfs_copy: bool | None = None


class VariantConfig(BaseModel):
    """Configuration for a variant"""
    name: str
    tag_suffix: str
    template: str | None = None
    versions: dict[str, str] = {}
    variables: dict[str, str] = {}
    rootfs_user: str | None = None
    rootfs_copy: bool | None = None


class ImageConfig(BaseModel):
    """Root configuration from image.yml"""
    name: str | None = None
    template: str | None = None
    versions: dict[str, str] = {}
    variables: dict[str, str] = {}
    tags: list[TagConfig]
    variants: list[VariantConfig] = []
    is_base_image: bool = False
    extends: str | None = None
    aliases: dict[str, str] = {}
    rootfs_user: str | None = None
    rootfs_copy: bool | None = None
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add manager/config.py tests/test_config.py
git commit -m "feat(config): add rootfs_user and rootfs_copy fields"
```

---

## Task 2: Add rootfs fields to domain models

**Files:**
- Modify: `manager/models.py:10-39`
- Test: `tests/test_models.py` (create if needed)

**Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
from pathlib import Path
from manager.models import Tag, Variant, Image


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
        template_path=Path("test.tpl"),
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
        template_path=Path("test.tpl"),
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py::test_tag_has_rootfs_fields -v`
Expected: FAIL with TypeError (unexpected keyword argument)

**Step 3: Write implementation**

Edit `manager/models.py`:

```python
@dataclass
class Tag:
    """Resolved tag with merged versions and variables"""
    name: str
    versions: dict[str, str]
    variables: dict[str, str]
    rootfs_user: str = "0:0"
    rootfs_copy: bool = True


@dataclass
class Variant:
    """Resolved variant with generated tags"""
    name: str
    template_path: Path
    tags: list[Tag]
    aliases: dict[str, str] = field(default_factory=dict)
    rootfs_user: str = "0:0"
    rootfs_copy: bool = True


@dataclass
class Image:
    """Fully resolved image with all computed data"""
    name: str
    path: Path
    template_path: Path
    versions: dict[str, str]
    variables: dict[str, str]
    tags: list[Tag]
    variants: list[Variant]
    is_base_image: bool
    extends: str | None
    aliases: dict[str, str]
    rootfs_user: str = "0:0"
    rootfs_copy: bool = True
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add manager/models.py tests/test_models.py
git commit -m "feat(models): add rootfs_user and rootfs_copy to domain models"
```

---

## Task 3: Implement rootfs settings inheritance in ModelResolver

**Files:**
- Modify: `manager/models.py:96-172`
- Test: `tests/test_models.py`

**Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
from manager.config import ConfigLoader
from manager.models import ModelResolver


def test_resolver_inherits_rootfs_from_image(tmp_path):
    """Test ModelResolver inherits rootfs settings from image to tags"""
    config_file = tmp_path / "image.yml"
    config_file.write_text("""
name: test
rootfs_user: "1000:1000"
rootfs_copy: false
tags:
  - name: "1.0"
""")
    (tmp_path / "Dockerfile.tmpl").write_text("FROM base")

    config = ConfigLoader.load(config_file)
    resolver = ModelResolver()
    image = resolver.resolve(config, tmp_path)

    assert image.rootfs_user == "1000:1000"
    assert image.rootfs_copy is False
    assert image.tags[0].rootfs_user == "1000:1000"
    assert image.tags[0].rootfs_copy is False


def test_resolver_tag_overrides_image_rootfs(tmp_path):
    """Test tag-level rootfs settings override image-level"""
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
    (tmp_path / "Dockerfile.tmpl").write_text("FROM base")

    config = ConfigLoader.load(config_file)
    resolver = ModelResolver()
    image = resolver.resolve(config, tmp_path)

    assert image.tags[0].rootfs_user == "0:0"
    assert image.tags[0].rootfs_copy is False


def test_resolver_variant_inherits_rootfs(tmp_path):
    """Test variant inherits rootfs from image, tag overrides cascade"""
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
    (tmp_path / "Dockerfile.tmpl").write_text("FROM base")
    (tmp_path / "Dockerfile.slim.tmpl").write_text("FROM base")

    config = ConfigLoader.load(config_file)
    resolver = ModelResolver()
    image = resolver.resolve(config, tmp_path)

    assert image.variants[0].rootfs_user == "0:0"
    assert image.variants[0].rootfs_copy is True  # Default, not overridden
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py::test_resolver_inherits_rootfs_from_image -v`
Expected: FAIL with assertion error (rootfs settings not inherited)

**Step 3: Write implementation**

Edit `manager/models.py` in `ModelResolver.resolve()` method (around line 96-106):

```python
# After building base_tags, update the tag creation to include rootfs settings:
base_tags = []
for tag_config in config.tags:
    merged_versions = Merger.merge(config.versions, tag_config.versions)
    merged_variables = Merger.merge(config.variables, tag_config.variables)

    # Inherit rootfs settings: image -> tag (later wins)
    tag_rootfs_user = tag_config.rootfs_user if tag_config.rootfs_user is not None else (config.rootfs_user or "0:0")
    tag_rootfs_copy = tag_config.rootfs_copy if tag_config.rootfs_copy is not None else (config.rootfs_copy if config.rootfs_copy is not None else True)

    base_tags.append(Tag(
        name=tag_config.name,
        versions=merged_versions,
        variables=merged_variables,
        rootfs_user=tag_rootfs_user,
        rootfs_copy=tag_rootfs_copy
    ))
```

Also update variant creation (around line 141-146):

```python
# Inherit rootfs settings for variant: image -> variant (later wins)
variant_rootfs_user = variant_config.rootfs_user if variant_config.rootfs_user is not None else (config.rootfs_user or "0:0")
variant_rootfs_copy = variant_config.rootfs_copy if variant_config.rootfs_copy is not None else (config.rootfs_copy if config.rootfs_copy is not None else True)

variants.append(Variant(
    name=variant_config.name,
    template_path=variant_template_path,
    tags=variant_tags,
    aliases=variant_aliases,
    rootfs_user=variant_rootfs_user,
    rootfs_copy=variant_rootfs_copy
))
```

And update the Image return (around line 161-172):

```python
# Image-level rootfs settings (with defaults)
image_rootfs_user = config.rootfs_user or "0:0"
image_rootfs_copy = config.rootfs_copy if config.rootfs_copy is not None else True

return Image(
    name=image_name,
    path=path,
    template_path=template_path,
    versions=config.versions,
    variables=config.variables,
    tags=base_tags,
    variants=variants,
    is_base_image=config.is_base_image,
    extends=config.extends,
    aliases=aliases,
    rootfs_user=image_rootfs_user,
    rootfs_copy=image_rootfs_copy
)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add manager/models.py tests/test_models.py
git commit -m "feat(models): implement rootfs settings inheritance in ModelResolver"
```

---

## Task 4: Create rootfs merging utility

**Files:**
- Create: `manager/rootfs.py`
- Test: `tests/test_rootfs.py`

**Step 1: Write the failing test**

Create `tests/test_rootfs.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rootfs.py::test_collect_rootfs_paths_image_level -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Write implementation**

Create `manager/rootfs.py`:

```python
"""Rootfs merging utilities for image builds."""

import shutil
from pathlib import Path


def collect_rootfs_paths(image_path: Path, version_path: Path, variant_name: str | None) -> list[Path]:
    """Collect rootfs directories from all levels in merge order.

    Order (later wins): image -> version -> variant

    Args:
        image_path: Path to image directory (e.g., images/python)
        version_path: Path to version directory (e.g., images/python/3)
        variant_name: Optional variant name

    Returns:
        List of existing rootfs paths in merge order
    """
    paths = []

    # Level 1: Image-wide rootfs
    image_rootfs = image_path / "rootfs"
    if image_rootfs.is_dir():
        paths.append(image_rootfs)

    # Level 2: Version-specific rootfs
    version_rootfs = version_path / "rootfs"
    if version_rootfs.is_dir():
        paths.append(version_rootfs)

    # Level 3: Variant-specific rootfs
    if variant_name:
        variant_rootfs = version_path / variant_name / "rootfs"
        if variant_rootfs.is_dir():
            paths.append(variant_rootfs)

    return paths


def has_rootfs_content(rootfs_paths: list[Path]) -> bool:
    """Check if any rootfs directory contains actual files (not just empty dirs).

    Args:
        rootfs_paths: List of rootfs directories to check

    Returns:
        True if any directory contains files
    """
    for rootfs_path in rootfs_paths:
        if not rootfs_path.exists():
            continue
        for item in rootfs_path.rglob("*"):
            if item.is_file() or item.is_symlink():
                return True
    return False


def merge_rootfs(rootfs_paths: list[Path], dest: Path) -> None:
    """Merge multiple rootfs directories into destination.

    Later directories in the list override earlier ones (later wins).
    Preserves symlinks.

    Args:
        rootfs_paths: List of rootfs directories in merge order
        dest: Destination directory for merged rootfs
    """
    if not rootfs_paths:
        return

    dest.mkdir(parents=True, exist_ok=True)

    for rootfs_path in rootfs_paths:
        if not rootfs_path.exists():
            continue

        for item in rootfs_path.rglob("*"):
            rel_path = item.relative_to(rootfs_path)
            dest_path = dest / rel_path

            if item.is_symlink():
                # Preserve symlinks
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                if dest_path.exists() or dest_path.is_symlink():
                    dest_path.unlink()
                dest_path.symlink_to(item.readlink())
            elif item.is_file():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest_path)
            elif item.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)


def warn_sensitive_files(rootfs_path: Path) -> list[str]:
    """Check for potentially sensitive files in rootfs.

    Args:
        rootfs_path: Path to rootfs directory

    Returns:
        List of warning messages for sensitive files found
    """
    sensitive_patterns = [".env", "*.key", "*.pem", "*.p12", "*.pfx", "id_rsa", "id_ed25519"]
    warnings = []

    if not rootfs_path.exists():
        return warnings

    for pattern in sensitive_patterns:
        for match in rootfs_path.rglob(pattern):
            warnings.append(f"Warning: potentially sensitive file in rootfs: {match}")

    return warnings
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rootfs.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add manager/rootfs.py tests/test_rootfs.py
git commit -m "feat(rootfs): add rootfs merging utilities"
```

---

## Task 5: Implement COPY injection in render_dockerfile

**Files:**
- Modify: `manager/rendering.py:92-116`
- Test: `tests/test_rendering.py` (create if needed)

**Step 1: Write the failing test**

Create `tests/test_rendering.py`:

```python
from pathlib import Path
from manager.rendering import inject_rootfs_copy


def test_inject_rootfs_copy_after_first_from():
    """Test COPY is injected after first FROM"""
    dockerfile = """FROM base:1.0
RUN apt-get update
"""
    result = inject_rootfs_copy(dockerfile, "0:0")
    expected = """FROM base:1.0
COPY --chown=0:0 rootfs/ /
RUN apt-get update
"""
    assert result == expected


def test_inject_rootfs_copy_with_custom_user():
    """Test COPY uses custom user"""
    dockerfile = "FROM base:1.0\nRUN echo hello"
    result = inject_rootfs_copy(dockerfile, "1000:1000")
    assert "COPY --chown=1000:1000 rootfs/ /" in result


def test_inject_rootfs_copy_multi_stage():
    """Test COPY is injected only after FIRST FROM in multi-stage"""
    dockerfile = """FROM builder:1.0 AS build
RUN make
FROM runtime:1.0
COPY --from=build /app /app
"""
    result = inject_rootfs_copy(dockerfile, "0:0")
    lines = result.split("\n")
    # COPY should be after first FROM, not after second
    assert lines[1] == "COPY --chown=0:0 rootfs/ /"
    assert "COPY --chown=0:0 rootfs/ /" not in "\n".join(lines[3:])


def test_inject_skips_if_already_present():
    """Test injection is skipped if COPY rootfs/ already exists"""
    dockerfile = """FROM base:1.0
COPY rootfs/ /
RUN echo hello
"""
    result = inject_rootfs_copy(dockerfile, "0:0")
    assert result == dockerfile  # Unchanged
    assert result.count("COPY") == 1


def test_inject_skips_if_copy_rootfs_with_chown():
    """Test injection is skipped if COPY --chown=X rootfs/ already exists"""
    dockerfile = """FROM base:1.0
COPY --chown=1000:1000 rootfs/ /custom/path
"""
    result = inject_rootfs_copy(dockerfile, "0:0")
    assert result == dockerfile


def test_inject_preserves_from_args():
    """Test injection works with FROM args"""
    dockerfile = """ARG BASE_IMAGE=base:1.0
FROM ${BASE_IMAGE}
RUN echo hello
"""
    result = inject_rootfs_copy(dockerfile, "0:0")
    lines = result.split("\n")
    assert lines[0] == "ARG BASE_IMAGE=base:1.0"
    assert lines[1] == "FROM ${BASE_IMAGE}"
    assert lines[2] == "COPY --chown=0:0 rootfs/ /"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rendering.py::test_inject_rootfs_copy_after_first_from -v`
Expected: FAIL with ImportError (inject_rootfs_copy not found)

**Step 3: Write implementation**

Add to `manager/rendering.py` (after the existing imports):

```python
import re


def inject_rootfs_copy(dockerfile: str, rootfs_user: str) -> str:
    """Inject COPY rootfs/ instruction after first FROM.

    Args:
        dockerfile: The Dockerfile content
        rootfs_user: User:group for COPY --chown

    Returns:
        Dockerfile with COPY injected, or unchanged if already present
    """
    # Skip if COPY rootfs/ already exists
    if re.search(r'COPY\s+.*rootfs/', dockerfile):
        return dockerfile

    # Find first FROM line and inject after it
    lines = dockerfile.split("\n")
    result = []
    injected = False

    for line in lines:
        result.append(line)
        if not injected and re.match(r'^\s*FROM\s+', line, re.IGNORECASE):
            result.append(f"COPY --chown={rootfs_user} rootfs/ /")
            injected = True

    return "\n".join(result)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rendering.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add manager/rendering.py tests/test_rendering.py
git commit -m "feat(rendering): add inject_rootfs_copy function"
```

---

## Task 6: Integrate rootfs in render_dockerfile

**Files:**
- Modify: `manager/rendering.py:92-116`
- Modify: `manager/rendering.py:39-46` (RenderContext)
- Test: `tests/test_rendering.py`

**Step 1: Write the failing test**

Add to `tests/test_rendering.py`:

```python
from manager.models import Image, Tag, Variant
from manager.rendering import RenderContext, render_dockerfile


def test_render_dockerfile_injects_copy_when_rootfs_exists(tmp_path):
    """Test render_dockerfile injects COPY when has_rootfs is True"""
    tpl = tmp_path / "Dockerfile.tmpl"
    tpl.write_text("FROM base:1.0\nRUN echo hello")

    image = Image(
        name="test",
        path=tmp_path,
        template_path=tpl,
        versions={},
        variables={},
        tags=[],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={},
        rootfs_user="1000:1000",
        rootfs_copy=True
    )
    tag = Tag(name="1.0", versions={}, variables={}, rootfs_user="1000:1000", rootfs_copy=True)

    ctx = RenderContext(image=image, tag=tag, all=[], has_rootfs=True)
    result = render_dockerfile(ctx)

    assert "COPY --chown=1000:1000 rootfs/ /" in result


def test_render_dockerfile_no_inject_when_rootfs_copy_false(tmp_path):
    """Test render_dockerfile skips injection when rootfs_copy is False"""
    tpl = tmp_path / "Dockerfile.tmpl"
    tpl.write_text("FROM base:1.0\nRUN echo hello")

    image = Image(
        name="test",
        path=tmp_path,
        template_path=tpl,
        versions={},
        variables={},
        tags=[],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={},
        rootfs_user="0:0",
        rootfs_copy=False
    )
    tag = Tag(name="1.0", versions={}, variables={}, rootfs_user="0:0", rootfs_copy=False)

    ctx = RenderContext(image=image, tag=tag, all=[], has_rootfs=True)
    result = render_dockerfile(ctx)

    assert "COPY" not in result


def test_render_dockerfile_no_inject_when_no_rootfs(tmp_path):
    """Test render_dockerfile skips injection when has_rootfs is False"""
    tpl = tmp_path / "Dockerfile.tmpl"
    tpl.write_text("FROM base:1.0\nRUN echo hello")

    image = Image(
        name="test",
        path=tmp_path,
        template_path=tpl,
        versions={},
        variables={},
        tags=[],
        variants=[],
        is_base_image=False,
        extends=None,
        aliases={},
    )
    tag = Tag(name="1.0", versions={}, variables={})

    ctx = RenderContext(image=image, tag=tag, all=[], has_rootfs=False)
    result = render_dockerfile(ctx)

    assert "COPY" not in result
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rendering.py::test_render_dockerfile_injects_copy_when_rootfs_exists -v`
Expected: FAIL with TypeError (has_rootfs not in RenderContext)

**Step 3: Write implementation**

Edit `manager/rendering.py` - update RenderContext:

```python
@dataclasses.dataclass(frozen=True)
class RenderContext:
    image: Image
    tag: Tag
    all: list[Image]
    variant: Variant | None = None
    snapshot_id: str | None = None
    has_rootfs: bool = False
```

Update `render_dockerfile` function:

```python
def render_dockerfile(context: RenderContext):
    env = Environment()
    env.filters["resolve_base_image"] = _resolve_base_image(context)
    env.filters["resolve_version"] = _resolve_version(context)

    variant_args = {}

    if context.variant is not None:
        # For variants, need to find the base tag name (without suffix)
        base_tag_name = context.tag.name
        for base_tag in context.image.tags:
            if context.tag.name.startswith(base_tag.name):
                base_tag_name = base_tag.name
                break

        variant_args = {
            "base_image": f"{context.image.name}:{base_tag_name}",
        }
        tpl_file = context.variant.template_path
        rootfs_user = context.variant.rootfs_user
        rootfs_copy = context.variant.rootfs_copy
    else:
        tpl_file = context.image.dockerfile_template_path
        rootfs_user = context.tag.rootfs_user
        rootfs_copy = context.tag.rootfs_copy

    tpl = env.from_string(tpl_file.read_text())
    rendered = tpl.render(image=context.image, tag=context.tag, **variant_args)

    # Inject COPY rootfs/ if conditions are met
    if context.has_rootfs and rootfs_copy:
        rendered = inject_rootfs_copy(rendered, rootfs_user)

    return rendered
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rendering.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add manager/rendering.py tests/test_rendering.py
git commit -m "feat(rendering): integrate rootfs COPY injection in render_dockerfile"
```

---

## Task 7: Integrate rootfs merging in cmd_generate

**Files:**
- Modify: `manager/__main__.py:87-181`
- Test: `tests/test_generate.py` (create if needed)

**Step 1: Write the failing test**

Create `tests/test_generate.py`:

```python
import subprocess
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
    (version_dir / "Dockerfile.tmpl").write_text("FROM base:1.0\nRUN echo hello")
    (version_dir / "test.yml.tpl").write_text("schemaVersion: '2.0.0'")

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
    (images_dir / "Dockerfile.tmpl").write_text("FROM base:1.0\nRUN echo hello")
    (images_dir / "test.yml.tpl").write_text("schemaVersion: '2.0.0'")

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
    (images_dir / "Dockerfile.tmpl").write_text("FROM base:1.0\nRUN echo hello")
    (images_dir / "test.yml.tpl").write_text("schemaVersion: '2.0.0'")

    (tmp_path / "dist").mkdir()
    monkeypatch.chdir(tmp_path)

    from manager.__main__ import cmd_generate
    cmd_generate([])

    dockerfile = (tmp_path / "dist" / "test" / "1.0" / "Dockerfile").read_text()
    assert "COPY" not in dockerfile
    assert not (tmp_path / "dist" / "test" / "1.0" / "rootfs").exists()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_generate.py::test_generate_merges_rootfs -v`
Expected: FAIL with assertion error (rootfs not merged)

**Step 3: Write implementation**

Edit `manager/__main__.py` - add import at top:

```python
from manager.rootfs import collect_rootfs_paths, merge_rootfs, has_rootfs_content, warn_sensitive_files
```

Update `cmd_generate` function - replace the tag rendering loop (around line 139-150):

```python
# Render base tags
for tag in image.tags:
    tag_out_path = image_out_path.joinpath(tag.name)
    tag_out_path.mkdir(parents=True, exist_ok=True)

    # Collect and merge rootfs
    rootfs_paths = collect_rootfs_paths(
        image_path=image.path.parent,  # images/python
        version_path=image.path,        # images/python/3
        variant_name=None
    )
    has_rootfs = has_rootfs_content(rootfs_paths)

    if has_rootfs:
        merged_rootfs = tag_out_path / "rootfs"
        merge_rootfs(rootfs_paths, merged_rootfs)
        # Warn about sensitive files
        for warning in warn_sensitive_files(merged_rootfs):
            print(warning, file=sys.stderr)

    ctx = RenderContext(
        image=image,
        all=sorted_images,
        tag=tag,
        variant=None,
        snapshot_id=snapshot_id,
        has_rootfs=has_rootfs
    )

    rendered_dockerfile = render_dockerfile(ctx)
    tag_out_path.joinpath("Dockerfile").write_text(rendered_dockerfile)

    rendered_test_config = render_test_config(ctx)
    tag_out_path.joinpath("test.yml").write_text(rendered_test_config)
```

And update the variant rendering loop (around line 152-164):

```python
# Render variant tags
for variant in image.variants:
    for variant_tag in variant.tags:
        variant_out_path = image_out_path.joinpath(variant_tag.name)
        variant_out_path.mkdir(parents=True, exist_ok=True)

        # Collect and merge rootfs (including variant-specific)
        rootfs_paths = collect_rootfs_paths(
            image_path=image.path.parent,
            version_path=image.path,
            variant_name=variant.name
        )
        has_rootfs = has_rootfs_content(rootfs_paths)

        if has_rootfs:
            merged_rootfs = variant_out_path / "rootfs"
            merge_rootfs(rootfs_paths, merged_rootfs)
            for warning in warn_sensitive_files(merged_rootfs):
                print(warning, file=sys.stderr)

        ctx = RenderContext(
            image=image,
            all=sorted_images,
            tag=variant_tag,
            variant=variant,
            snapshot_id=snapshot_id,
            has_rootfs=has_rootfs
        )

        rendered_dockerfile = render_dockerfile(ctx)
        variant_out_path.joinpath("Dockerfile").write_text(rendered_dockerfile)

        rendered_test_config = render_test_config(ctx)
        variant_out_path.joinpath("test.yml").write_text(rendered_test_config)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_generate.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add manager/__main__.py tests/test_generate.py
git commit -m "feat(generate): integrate rootfs merging and COPY injection"
```

---

## Task 8: Run full test suite and verify

**Files:**
- All modified files

**Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests PASS

**Step 2: Run integration test with real images**

```bash
# Add rootfs to an existing image for testing
mkdir -p images/python/rootfs/etc
echo "# Python config" > images/python/rootfs/etc/python.conf

mkdir -p images/python/3/rootfs/usr/local/bin
echo "#!/bin/sh\necho hello" > images/python/3/rootfs/usr/local/bin/hello.sh

# Generate and verify
uv run image-manager generate

# Check output
cat dist/python/3.13.7/Dockerfile | grep "COPY"
ls dist/python/3.13.7/rootfs/
```

Expected:
- Dockerfile contains `COPY --chown=0:0 rootfs/ /`
- rootfs/ contains merged files from both levels

**Step 3: Clean up test files**

```bash
rm -rf images/python/rootfs images/python/3/rootfs
```

**Step 4: Final commit**

```bash
git add -A
git commit -m "test: verify rootfs support integration"
```

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Add rootfs fields to config models | config.py, test_config.py |
| 2 | Add rootfs fields to domain models | models.py, test_models.py |
| 3 | Implement rootfs inheritance in ModelResolver | models.py, test_models.py |
| 4 | Create rootfs merging utility | rootfs.py, test_rootfs.py |
| 5 | Implement COPY injection function | rendering.py, test_rendering.py |
| 6 | Integrate rootfs in render_dockerfile | rendering.py, test_rendering.py |
| 7 | Integrate rootfs in cmd_generate | __main__.py, test_generate.py |
| 8 | Full test suite verification | All files |

Total: 8 tasks with TDD approach
