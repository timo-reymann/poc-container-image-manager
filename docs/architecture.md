# Architecture

## Overview

The image manager uses a three-layer architecture:

**Config Layer** → **Model Layer** → **Rendering Layer**

### Config Layer (`manager/config.py`)

Loads and validates YAML files using Pydantic:
- `ImageConfig` - Root configuration
- `TagConfig` - Individual tag config
- `VariantConfig` - Variant config
- `ConfigLoader` - YAML loader

No business logic - just validation and parsing.

### Model Layer (`manager/models.py`)

Transforms configs into resolved domain models:
- `Image` - Fully resolved image with computed data
- `Tag` - Tag with merged versions/variables
- `Variant` - Variant with generated tags
- `ModelResolver` - Transformation engine

This layer handles:
- Template resolution (explicit → variant → default)
- Version/variable merging (image → tag → variant)
- Variant tag generation (base tags + suffix)

### Rendering Layer (`manager/rendering.py`)

Generates output files from resolved models:
- Receives complete data (no late binding)
- Renders Jinja2 templates
- Writes Dockerfiles and test configs

## Data Flow

```
image.yml → ConfigLoader → ImageConfig
                              ↓
                        ModelResolver
                              ↓
                           Image (with Tags and Variants)
                              ↓
                          Renderer
                              ↓
                      Dockerfile + test.yml
```

## Template Resolution

Discovery order:
1. Explicit template from config
2. Variant-specific: `Dockerfile.{variant}.tmpl`
3. Default: `Dockerfile.tmpl`

## Variable Merging

Override cascade (later wins):
- Image → Tag → Variant

Both `versions` and `variables` use same merging strategy.

## Variant Tags

Variants inherit ALL base tags and apply suffix:
- Base: `["3.13.7", "3.13.6"]`
- Variant "browser" with suffix "-browser"
- Result: `["3.13.7-browser", "3.13.6-browser"]`

Each variant tag has fully merged versions/variables.

## Automatic Alias Generation

The system automatically generates semantic version aliases without manual configuration.

### AliasGenerator (`manager/alias_generator.py`)

Parses tags, detects semantic versions, and generates prefix-level aliases:
- `parse_semver(tag_name)` - Extracts (major, minor, patch) from tag names
- `generate_semver_aliases(tags)` - Creates alias mappings

### Alias Generation Rules

For tags like `9.0.100`, `9.0.200`, `9.1.50`:
- Major alias: `9` → `9.1.50` (highest 9.x.x)
- Minor aliases: `9.0` → `9.0.200`, `9.1` → `9.1.50`

Non-semver tags (like `latest`) are silently skipped.

### Variant Aliases

Variants automatically get aliases with suffix:
- Variant tags: `9.0.100-semantic`, `9.0.200-semantic`
- Aliases: `9-semantic` → `9.0.200-semantic`, `9.0-semantic` → `9.0.200-semantic`

### Integration

ModelResolver calls `generate_semver_aliases()` after building tags.
Aliases are stored in `Image.aliases` and `Variant.aliases` dicts.

## Build Infrastructure

### Container Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Host Machine                             │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  buildkitd   │  │   registry   │  │    garage    │       │
│  │  (rootless)  │  │  (registry:2)│  │   (S3 cache) │       │
│  │  :8372       │  │  :5050       │  │   :3900      │       │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │
│         │                 │                 │                │
│         └────────────┬────┴─────────────────┘                │
│                      │                                       │
│              ┌───────▼───────┐                              │
│              │    buildctl   │                              │
│              │  (local bin)  │                              │
│              └───────────────┘                              │
│                                                              │
│  ┌──────────────┐                                           │
│  │     dind     │  ← Testing only                           │
│  │  (isolated)  │                                           │
│  │  :2375       │                                           │
│  └──────────────┘                                           │
└─────────────────────────────────────────────────────────────┘
```

### buildkitd (`manager/building.py`)

Handles container image builds using BuildKit:

- **Linux**: Runs natively using bundled `buildkitd` binary
- **macOS**: Runs rootless in Docker container (`moby/buildkit:rootless`)
  - Uses `--oci-worker-no-process-sandbox` flag
  - No privileged mode required
  - Security options: `seccomp=unconfined`, `apparmor=unconfined`

Key functions:
- `start_buildkitd()` - Starts daemon (native or container)
- `run_build()` - Executes buildctl with S3 cache support
- `rewrite_dockerfile_for_registry()` - Rewrites FROM lines for local base images

### Registry (`manager/building.py`)

Local registry for base image resolution between builds:

- Container: `registry:2` on port 5050
- Built images are pushed via `crane push`
- Dependent images pull bases from `host.docker.internal:5050` (macOS) or `localhost:5050` (Linux)
- Configured as insecure in buildkitd for HTTP access

### Garage S3 Cache (`manager/building.py`)

S3-compatible storage for BuildKit layer caching:

- Container: `dxflrs/garage:v2.1.0` on port 3900
- Single-node setup with automatic initialization
- Credentials stored in `.buildkit/garage/credentials.json`
- Cache shared by image name (not tag) for better reuse

Initialization flow:
1. Start container with generated config
2. Assign node role and apply layout
3. Create bucket `buildkit-cache`
4. Create access key and save credentials
5. Grant bucket permissions

### dind (`manager/testing.py`)

Docker-in-Docker for isolated test execution:

- **Linux**: Minimal capabilities (`SYS_ADMIN`, `NET_ADMIN`, `MKNOD`)
- **macOS**: Privileged mode (required due to Docker Desktop VM cgroup limitations)

Test flow:
1. Load image tar into dind daemon
2. Run `container-structure-test` against dind
3. Tests execute in isolated environment

## Build Flow

```
1. Generate     images/*.yml → dist/<name>/<tag>/Dockerfile
                            → dist/<name>/<tag>/test.yml

2. Build        Dockerfile → buildctl → image.tar
                          ↓
                    S3 cache (import/export)
                          ↓
                    Push to registry

3. Test         image.tar → dind load → container-structure-test
```

## Dependency Resolution

Images are sorted topologically based on FROM dependencies:

1. `extract_dependencies()` - Parses Dockerfiles for FROM lines
2. `sort_images()` - Topological sort using Kahn's algorithm
3. Build order ensures base images exist before dependents

