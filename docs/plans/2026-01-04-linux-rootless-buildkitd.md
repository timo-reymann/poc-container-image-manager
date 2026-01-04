# Native Rootless buildkitd on Linux

## Overview

Modify `start_buildkitd_native()` to wrap buildkitd with rootlesskit, enabling unprivileged builds on Linux. The rootlesskit binary will be bundled at `bin/linux-amd64/rootlesskit`.

## Changes to `manager/building.py`

Add helper to locate rootlesskit:

```python
def get_rootlesskit_path() -> Path:
    """Get the path to the rootlesskit binary (Linux only)."""
    binary = get_bin_path() / "rootlesskit"
    if not binary.exists():
        raise RuntimeError(f"rootlesskit binary not found: {binary}")
    return binary
```

Update `start_buildkitd_native()` to wrap buildkitd with rootlesskit:

```python
def start_buildkitd_native() -> int:
    """Start buildkitd natively with rootlesskit (Linux only)."""
    # ... existing checks ...

    rootlesskit = get_rootlesskit_path()
    buildkitd = get_buildkitd_path()

    cmd = [
        str(rootlesskit),
        "--net=host",
        "--copy-up=/etc",
        "--copy-up=/run",
        str(buildkitd),
        "--addr", get_socket_addr(),
        "--root", str(DEFAULT_BUILDKIT_DIR / "root"),
        "--oci-worker-no-process-sandbox",
    ]
```

## Configuration File

Generate buildkitd config for insecure local registry (matching macOS behavior):

```python
config_dir = DEFAULT_BUILDKIT_DIR / "config"
config_dir.mkdir(parents=True, exist_ok=True)
config_file = config_dir / "buildkitd.toml"

registry_host = f"localhost:{REGISTRY_PORT}"
config_file.write_text(f'''
[registry."{registry_host}"]
  http = true
  insecure = true
''')

# Add to cmd:
cmd.extend(["--config", str(config_file)])
```

## Binary Bundling

Download rootlesskit from https://github.com/rootless-containers/rootlesskit/releases and place at:

```
bin/linux-amd64/rootlesskit
```

Track with Git LFS (update `.gitattributes`).

## Error Handling

- **Missing binary**: Raise `RuntimeError: rootlesskit binary not found: bin/linux-amd64/rootlesskit`
- **Kernel requirements**: rootlesskit requires user namespaces (kernel 4.18+). Errors from rootlesskit are surfaced directly.
- **No fallback**: Fail fast with clear error if rootlesskit unavailable.

## Summary of Changes

| File | Change |
|------|--------|
| `bin/linux-amd64/rootlesskit` | New bundled binary |
| `.gitattributes` | Add LFS tracking for rootlesskit |
| `manager/building.py` | Add `get_rootlesskit_path()`, update `start_buildkitd_native()` |
| `README.md` | Update Linux section to mention rootless |
