Proof of Concept for (CI) image management
===

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
- `build [image:tag] [--no-cache]` - Build image(s) to `dist/<name>/<tag>/image.tar`
- `test [image:tag]` - Test image(s) using the tar archive
- `start [daemon]` - Start daemons (buildkitd, registry, garage, dind, or all)
- `stop [daemon]` - Stop daemons
- `status [daemon]` - Check daemon status

When no image is specified for `build` or `test`, all images are processed in dependency order.

Output in dist/:
- `Dockerfile` - Generated Dockerfile
- `test.yml` - Test configuration
- `image.tar` - Built image (after build)

## Example

```shell
# Generate Dockerfiles and test configs
uv run image-manager generate

# Build and test all images (in dependency order)
uv run image-manager build
uv run image-manager build --no-cache  # Build without S3 cache
uv run image-manager test

# Or build and test specific images
uv run image-manager build base:2025.9
uv run image-manager test base:2025.9

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

## Features

- Uses yaml and subfolders by convention to create images
- Create matrix of variants and tags for each image
- Supports layering images via variants
- Allows supporting multiple tag hierarchies
- **Automatic semantic version aliases** - Generates all prefix-level aliases from tags
- Integration with container-structure-test for testing containers
- **S3-based build caching** - Uses Garage for fast incremental builds

## Missing features

- Configurable external registry endpoint and credentials
- Configurable external S3 endpoint and credentials
- CI pipeline generation (GitHub Actions, GitLab CI, etc.)
- More intelligent version parsing and sorting (potentially via strategy that can be specified)

## Implementation

- **Three-layer architecture**: Config → Model → Rendering
- **Config layer**: Pydantic models for YAML validation
- **Model layer**: Business logic for merging and resolution
- **Rendering layer**: Jinja2 template generation
- **Smart template discovery**: Convention with explicit overrides
- **Variable merging**: Override cascade from image → tag → variant
- **Variant tags**: Automatic generation with suffix-based naming

See [docs/architecture.md](docs/architecture.md) for details.