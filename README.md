Proof of Concept for (CI) image management
===

> **Note:** This is currently a conceptual proof of concept. The goal is to make this work out of the box on any CI provider (GitHub Actions, GitLab CI, etc.) and locally on macOS and Linux, where the entire toolchain runs in a prebuilt rootless container with no host dependencies beyond Docker.
>
> This codebase was heavily generated using [Claude Code](https://claude.ai/code). Code quality is not a priority - the focus is on demonstrating the overall concept and combining building blocks.

Proof of concept for easy (CI) image management, which is also transferable to any kind of prebuilt images provided;
e.g. runtime images.

## Requirements

- Docker
- uv
- Python 3.13

## Bundled Tools

The following tools are bundled in `bin/` for linux-amd64 and darwin-arm64:

| Tool | Version | Purpose |
|------|---------|---------|
| [crane](https://github.com/google/go-containerregistry) | v0.20.7 | Multi-tagging images |
| [container-structure-test](https://github.com/GoogleContainerTools/container-structure-test) | v1.22.1 | Image testing |
| [syft](https://github.com/anchore/syft) | v1.39.0 | SBOM generation |
| [buildkit](https://github.com/moby/buildkit) | v0.26.3 | Rootless builds |

## Usage

```shell
uv sync
uv run image-manager <command> [args]
```

Commands:
- `generate` - Generate Dockerfiles and test configs from `images/`
- `build [image:tag] [options]` - Build image(s) to `dist/<name>/<tag>/image.tar`
- `manifest <image:tag>` - Create multi-platform manifest from registry images
- `sbom [image:tag] [--format FORMAT]` - Generate SBOM for image(s)
- `test [image:tag]` - Test image(s) using the tar archive
- `start [daemon]` - Start daemons (buildkitd, registry, garage, dind, or all)
- `stop [daemon]` - Stop daemons
- `status [daemon]` - Check daemon status

Build options:
  --no-cache          Disable S3 build cache
  --platform PLAT     Build for specific platform only (amd64, arm64)
                      Default: builds linux/amd64 + linux/arm64 with multi-platform manifest

Manifest options:
  --snapshot-id ID    Use snapshot ID suffix for registry tags

When no image is specified for `build` or `test`, all images are processed in dependency order.

Output in dist/:
- `index.html` - Image catalog with all images, tags, versions, and platform badges
- `<name>/<tag>/index.html` - Tag report with platform details and SBOM links
- `<name>/<tag>/linux-amd64/image.tar` - AMD64 platform image
- `<name>/<tag>/linux-arm64/image.tar` - ARM64 platform image
- `<name>/<tag>/image.tar` - Multi-platform OCI index (or single platform copy)
- `<name>/<tag>/Dockerfile` - Generated Dockerfile
- `<name>/<tag>/test.yml` - Test configuration
- `<name>/<tag>/rootfs/` - Merged rootfs files (if any)
- `<name>/<tag>/linux-*/sbom.cyclonedx.json` - SBOM per platform

## Example

```shell
# Generate Dockerfiles and test configs
uv run image-manager generate

# Build, generate SBOM, and test all images (in dependency order)
uv run image-manager build
uv run image-manager sbom
uv run image-manager test

# Or work with specific images
uv run image-manager build base:2025.09
uv run image-manager sbom base:2025.09
uv run image-manager test base:2025.09

# Build without S3 cache
uv run image-manager build --no-cache

# Build for specific platform
uv run image-manager build base:2025.09 --platform amd64
uv run image-manager build base:2025.09 --platform arm64

# Build all platforms (default, uses emulation for non-native)
uv run image-manager build base:2025.09

# Create manifest from existing registry images
uv run image-manager manifest base:2025.09

# Generate SBOM in different formats
uv run image-manager sbom --format spdx-json   # SPDX format
uv run image-manager sbom --format json        # Syft native format

# Stop daemons when done
uv run image-manager stop
```

### Daemon management

Daemons are started automatically when needed, or can be managed manually:

```shell
uv run image-manager start             # Start all daemons
uv run image-manager start buildkitd   # Start only buildkitd
uv run image-manager start registry    # Start only registry
uv run image-manager start garage      # Start only garage (S3 cache)
uv run image-manager status            # Check status of all
uv run image-manager stop              # Stop all
```

**buildkitd** (for building):
- **Linux**: Runs natively using the bundled binary
- **macOS**: Runs rootless in a Docker container (`moby/buildkit:rootless`)

**registry** (for base image resolution):
- Local registry container (`registry:2`) on port 5050
- Built images are automatically pushed to the registry
- Dependent images pull their base from the registry (no host Docker dependency)

**dind** (for testing):
- **Linux**: Runs with minimal capabilities (`SYS_ADMIN`, `NET_ADMIN`, `MKNOD`)
- **macOS**: Runs in Docker Desktop (requires privileged due to VM cgroup limitations)
- Images are loaded from tar archives into the isolated daemon

**garage** (for build caching):
- S3-compatible storage container (`dxflrs/garage`) on port 3900
- Provides build layer caching for BuildKit
- Automatically used during builds (disable with `--no-cache`)

### Production considerations

The local containerized registry and S3 cache are for development/PoC purposes. In production:

**Registry** → External container registry (e.g., Harbor, ECR, GCR, Docker Hub)
- Configure via registry endpoint and credentials
- Base images pushed to and pulled from shared registry
- Stores both release tags and CI snapshot tags

**S3 Cache** → External S3-compatible storage (e.g., AWS S3, MinIO, Cloudflare R2)
- Same cache bucket used locally and in CI for shared layer caching
- Local builds benefit from CI-cached layers and vice versa
- Significantly speeds up both local iteration and CI pipelines

```
┌─────────────┐     ┌─────────────┐
│   Local     │     │     CI      │
│   Build     │     │   Pipeline  │
└──────┬──────┘     └──────┬──────┘
       │                   │
       └───────┬───────────┘
               ▼
       ┌───────────────┐
       │   S3 Cache    │  ← Shared layer cache
       │  (external)   │
       └───────────────┘
               │
               ▼
       ┌───────────────┐
       │   Registry    │  ← Final images
       │  (external)   │
       └───────────────┘
```

### Snapshot builds (CI pipelines)

Use `--snapshot-id` across all commands for MR/branch pipelines:

```shell
# MR/branch pipeline: full workflow with snapshot ID
uv run image-manager generate --snapshot-id "mr-${MR_ID}"  # FROM refs use snapshot
uv run image-manager build --snapshot-id "mr-${MR_ID}"     # Push snapshot tags only
uv run image-manager test --snapshot-id "mr-${MR_ID}"      # Test with snapshot context

# Main pipeline: release workflow (no snapshot)
uv run image-manager generate   # FROM refs use release tags
uv run image-manager build      # Push release tags
uv run image-manager test       # Test release
```

How `--snapshot-id` affects each command:
- **generate**: Dependent images reference snapshot base tags (e.g., `FROM base:2025.09-mr-123`)
- **build**: Pushes to registry with snapshot tag only (e.g., `base:2025.09-mr-123`)
- **test**: Logs snapshot context for traceability

This enables a clean promotion workflow:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   MR/Branch     │     │     Main        │     │    Registry     │
│   Pipeline      │     │   Pipeline      │     │                 │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                       │
         │ --snapshot-id mr-123  │                       │
         ├──────────────────────────────────────────────►│
         │                       │      base:2025.09-mr-123
         │                       │      python:3.13-mr-123
         │                       │                       │
         │    (merge to main)    │                       │
         │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─►│                       │
         │                       │ (no snapshot-id)      │
         │                       ├──────────────────────►│
         │                       │      base:2025.09     │
         │                       │      python:3.13      │
```

Benefits:
- **Isolation**: MR builds don't overwrite release tags
- **Traceability**: Every MR has unique, immutable snapshot tags
- **Clean promotion**: Main builds release tags, MRs build snapshots
- **Parallel safety**: Multiple MRs don't conflict
- **Dependency consistency**: Generated Dockerfiles reference correct snapshot bases

### Native multi-platform builds (CI)

For faster builds, use native runners for each architecture instead of emulation:

```shell
# On arm64 runner:
uv run image-manager build base:2025.09 --platform arm64

# On amd64 runner:
uv run image-manager build base:2025.09 --platform amd64

# On merge step (any runner):
uv run image-manager manifest base:2025.09
```

With snapshot IDs:

```shell
# On arm64 runner:
uv run image-manager build base:2025.09 --platform arm64 --snapshot-id mr-123

# On amd64 runner:
uv run image-manager build base:2025.09 --platform amd64 --snapshot-id mr-123

# Merge step:
uv run image-manager manifest base:2025.09 --snapshot-id mr-123
```

The `manifest` command:
1. Checks which platform images exist in registry (`base:2025.09-linux-amd64`, `base:2025.09-linux-arm64`)
2. Creates multi-platform OCI index using `crane index append`
3. Pushes manifest as `base:2025.09`
4. Exports manifest to `dist/base/2025.09/image.tar`

```
┌─────────────────┐     ┌─────────────────┐
│   arm64 Runner  │     │   amd64 Runner  │
│  --platform     │     │  --platform     │
│     arm64       │     │     amd64       │
└────────┬────────┘     └────────┬────────┘
         │                       │
         │  base:tag-linux-arm64 │  base:tag-linux-amd64
         └───────────┬───────────┘
                     ▼
             ┌───────────────┐
             │   Registry    │
             └───────┬───────┘
                     │
                     ▼
             ┌───────────────┐
             │  Merge Step   │
             │   manifest    │
             └───────┬───────┘
                     │
                     ▼  base:tag (multi-platform)
             ┌───────────────┐
             │   Registry    │
             └───────────────┘
```

## Features

- Uses yaml and subfolders by convention to create images
- Create matrix of variants and tags for each image
- Supports layering images via variants
- Allows supporting multiple tag hierarchies
- **Automatic semantic version aliases** - Generates all prefix-level aliases from tags
- **Rootfs file injection** - Copy static files into images with layered merging
- Integration with container-structure-test for testing containers
- **S3-based build caching** - Uses Garage for fast incremental builds

## Missing features

- Configurable external registry endpoint and credentials
- Configurable external S3 endpoint and credentials
- CI pipeline generation (GitHub Actions, GitLab CI, etc.)
- More intelligent version parsing and sorting (potentially via strategy that can be specified)

## Open questions

**Rootless testing**: Everything except `test` runs fully rootless:

| Command | Rootless | Notes |
|---------|----------|-------|
| `generate` | ✅ | Pure Python |
| `build` | ✅ | BuildKit is rootless |
| `sbom` | ✅ | Syft scans tar directly |
| `test` | ❌ | commandTests need container runtime |

Options being considered:
- Use Podman rootless with vfs storage driver (needs `seccomp=unconfined`)
- Limit local testing to file/metadata tests only (no commandTests)
- Run commandTests in CI only where dind/Podman is available
- Make `build` optional so users can build with their own tooling and only use the manager for generate/sbom

## Architecture

### Three-Layer Architecture

**Config Layer** → **Model Layer** → **Rendering Layer**

```
image.yml → ConfigLoader → ImageConfig
                              ↓
                        ModelResolver
                              ↓
                           Image (with Tags and Variants)
                              ↓
                          Renderer
                              ↓
                      Dockerfile + test.yml + index.html
```

**Config Layer** (`manager/config.py`): Loads and validates YAML files using Pydantic. No business logic - just validation and parsing.

**Model Layer** (`manager/models.py`): Transforms configs into resolved domain models. Handles template resolution, version/variable merging, and variant tag generation.

**Rendering Layer** (`manager/rendering.py`): Generates output files from resolved models using Jinja2 templates.

### Template Resolution

Discovery order:
1. Explicit template from config
2. Variant-specific: `Dockerfile.{variant}.tmpl`
3. Default: `Dockerfile.tmpl`

### Variable Merging

Override cascade (later wins): Image → Tag → Variant

Both `versions` and `variables` use same merging strategy.

### Variant Tags

Variants inherit ALL base tags and apply suffix:
- Base: `["3.13.7", "3.13.6"]`
- Variant "browser" with suffix "-browser"
- Result: `["3.13.7-browser", "3.13.6-browser"]`

### Automatic Alias Generation

The system automatically generates semantic version aliases without manual configuration.

For tags like `9.0.100`, `9.0.200`, `9.1.50`:
- Major alias: `9` → `9.1.50` (highest 9.x.x)
- Minor aliases: `9.0` → `9.0.200`, `9.1` → `9.1.50`

Variants automatically get aliases with suffix:
- Variant tags: `9.0.100-semantic`, `9.0.200-semantic`
- Aliases: `9-semantic` → `9.0.200-semantic`

### Rootfs File Injection

Copy static files into images using layered `rootfs/` directories. Files are merged using "later wins" semantics and automatically injected via `COPY` instruction.

#### Directory Structure

```
images/
└── python/
    ├── rootfs/                    # Image level (lowest priority)
    │   └── etc/
    │       ├── python-info        # Will be overridden by version
    │       └── image-level-only   # Unique to image level
    └── 3/
        ├── rootfs/                # Version level (higher priority)
        │   └── etc/
        │       ├── python-info    # Overrides image level
        │       └── version-only   # Unique to version level
        ├── semantic-release/
        │   └── rootfs/            # Variant level (highest priority)
        │       └── etc/
        │           └── variant-only
        └── image.yml
```

#### Merge Order (Later Wins)

1. **Image level**: `images/<name>/rootfs/`
2. **Version level**: `images/<name>/<version>/rootfs/`
3. **Variant level**: `images/<name>/<version>/<variant>/rootfs/`

Files from later levels replace files from earlier levels at the same path.

#### Configuration Options

In `image.yml`:

```yaml
# Image-level defaults
rootfs_user: "0:0"    # Owner for COPY --chown (default: "0:0")
rootfs_copy: true     # Whether to inject COPY instruction (default: true)

tags:
  - name: 3.13.7
    rootfs_user: "1000:1000"  # Override for specific tag
    rootfs_copy: false         # Disable injection for this tag

variants:
  - name: semantic-release
    rootfs_user: "node:node"   # Override for variant
```

#### Generated Output

When rootfs content exists and `rootfs_copy: true`:

**Dockerfile** (COPY injected after first FROM):
```dockerfile
FROM base:2025.09
COPY --chown=0:0 rootfs/ /
USER 0
...
```

**dist/ structure**:
```
dist/python/3.13.7/
├── Dockerfile           # With COPY instruction
├── rootfs/              # Merged files from all levels
│   └── etc/
│       ├── python-info      # Version-level content (later wins)
│       ├── image-level-only # From image level
│       └── version-only     # From version level
└── test.yml
```

#### Testing Rootfs Content

Use container-structure-test to verify files:

```yaml
# test.yml.tpl
schemaVersion: 2.0.0
fileExistenceTests:
  - name: "config-exists"
    path: "/etc/python-info"
    shouldExist: true

fileContentTests:
  - name: "config-has-version-content"
    path: "/etc/python-info"
    expectedContents: ["level=version"]
```

#### Decision Table

| Has rootfs content? | rootfs_copy | COPY injected? |
|---------------------|-------------|----------------|
| No                  | true        | No             |
| No                  | false       | No             |
| Yes                 | true        | Yes            |
| Yes                 | false       | No             |

#### Special Behaviors

- **Symlinks preserved**: Symlinks in rootfs are copied as symlinks
- **File replaces symlink**: Regular files from later levels replace symlinks from earlier levels
- **Sensitive file warnings**: Files matching patterns like `.env`, `*.key`, `*.pem` generate warnings during generation
- **Existing COPY skipped**: If Dockerfile already contains `COPY rootfs/`, injection is skipped

### Build Flow

```
1. Generate     images/*.yml → dist/<name>/<tag>/Dockerfile
                            → dist/<name>/<tag>/test.yml
                            → dist/index.html (catalog)

2. Build        For each platform (or single with --platform):
                  Dockerfile → buildctl --opt platform=X → linux-X/image.tar
                            ↓
                      S3 cache (import/export)
                            ↓
                      Push to registry (tag-linux-X)

                If multiple platforms:
                  crane index append → image.tar (multi-platform)
                            ↓
                      Push manifest to registry (tag)

3. Manifest     (Alternative to building both platforms locally)
                Check registry for platform images
                            ↓
                crane index append → image.tar (multi-platform)
                            ↓
                Push manifest to registry

4. SBOM         linux-X/image.tar → syft scan → linux-X/sbom.cyclonedx.json

5. Test         linux-X/image.tar → dind load → container-structure-test
```

### Container Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Host Machine                            │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  buildkitd   │  │   registry   │  │    garage    │      │
│  │  (rootless)  │  │  (registry:2)│  │   (S3 cache) │      │
│  │  :8372       │  │  :5050       │  │   :3900      │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                             │
│  ┌──────────────┐                                          │
│  │     dind     │  ← Testing only                          │
│  │  (isolated)  │                                          │
│  │  :2375       │                                          │
│  └──────────────┘                                          │
└─────────────────────────────────────────────────────────────┘
```

### SBOM Generation

Generates Software Bill of Materials using [syft](https://github.com/anchore/syft):
- Scans docker archives (`image.tar`) for installed packages
- Default format: CycloneDX JSON (industry standard for vulnerability scanning)

| Format | Output File | Use Case |
|--------|-------------|----------|
| `cyclonedx-json` | `sbom.cyclonedx.json` | Default, vulnerability scanning |
| `spdx-json` | `sbom.spdx.json` | License compliance |
| `json` | `sbom.syft.json` | Syft-specific tooling |

### Dependency Resolution

Images are sorted topologically based on FROM dependencies using Kahn's algorithm. Build order ensures base images exist before dependents.