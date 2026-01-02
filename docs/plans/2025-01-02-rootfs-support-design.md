# rootfs Support Design

## Overview

Add rootfs support to allow copying files into images at build time from a layered directory structure.

## Directory Hierarchy

Files are merged with later levels overriding earlier (later wins):

```
images/python/
├── rootfs/                    # Level 1: Image-wide
├── 3/
│   ├── rootfs/                # Level 2: Version-specific
│   ├── image.yml
│   └── semantic-release/
│       └── rootfs/            # Level 3: Variant-specific
```

## Configuration

```yaml
# image.yml
rootfs_user: "1000:1000"  # Optional, default "0:0"
rootfs_copy: true         # Optional, default true. Set false to disable auto-inject

tags:
  - name: 3.13.7
    rootfs_user: "0:0"
    rootfs_copy: false    # Disable for this tag (manual COPY in template)

variants:
  - name: semantic-release
    rootfs_user: "1000:1000"
    rootfs_copy: true
```

Inheritance chain: image → tag → variant (later wins, same as versions/variables)

## Implementation Flow

During `generate` command:

1. **Detect rootfs directories** for the image/tag/variant being generated
2. **Merge files** into a single rootfs in the build context:
   - Copy image-level rootfs first (if exists)
   - Overlay version-level rootfs (overwrites conflicts)
   - Overlay variant-level rootfs (overwrites conflicts)
3. **Write merged rootfs** to `dist/<name>/<tag>/rootfs/`
4. **Inject COPY** into generated Dockerfile after first `FROM` (if applicable)

## Auto-Injection Logic

In order:

1. If no rootfs exists at any level → **skip injection** (no files to copy)
2. If `rootfs_copy: false` → **skip injection** (explicitly disabled)
3. If template contains `COPY rootfs/` → **skip injection** (already handled)
4. Otherwise → **inject** `COPY --chown=X:X rootfs/ /` after first `FROM`

## Injection Format

```dockerfile
FROM base:2025.09
COPY --chown=0:0 rootfs/ /
# ... rest of Dockerfile
```

## Output Structure

```
dist/python/3.13.7/
├── Dockerfile          # With injected COPY (if applicable)
├── rootfs/             # Merged from all levels
│   └── etc/
│       └── config.ini
└── test.yml
```

## Decision Table

| rootfs exists? | rootfs_copy | template has COPY | Result |
|----------------|-------------|-------------------|--------|
| No | (any) | (any) | No injection, no rootfs/ in dist |
| Yes | false | (any) | rootfs/ in dist, no injection |
| Yes | true | Yes | rootfs/ in dist, no injection |
| Yes | true | No | rootfs/ in dist, **inject COPY** |

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No rootfs at any level | No injection, no rootfs/ in dist |
| Empty rootfs directories | Skip (no files to copy) |
| Only variant has rootfs | Only variant files copied |
| Symlinks in rootfs | Preserved as symlinks |
| Binary files | Copied as-is |
| Hidden files (`.bashrc`) | Included |

## Validation

Warn if rootfs contains files that look sensitive (`.env`, `*.key`, `*.pem`)
