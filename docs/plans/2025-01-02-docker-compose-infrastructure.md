# Docker Compose Infrastructure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract Garage and Registry into Docker Compose with a registry UI, removing Python container management code.

**Architecture:** Three services in docker-compose.yml (garage, registry, registry-ui) with fixed credentials. Python code connects to services but doesn't manage their lifecycle. start/stop/status CLI commands remain for buildkitd and dind only.

**Tech Stack:** Docker Compose, Garage v2.1.0, registry:2, joxit/docker-registry-ui

---

### Task 1: Create Garage Configuration

**Files:**
- Create: `infrastructure/garage.toml`

**Step 1: Create infrastructure directory and garage.toml**

```toml
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "lmdb"
replication_factor = 1
consistency_mode = "consistent"

[rpc]
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[s3_api]
api_bind_addr = "[::]:3900"
s3_region = "garage"
root_domain = ".s3.garage"

[admin]
api_bind_addr = "0.0.0.0:3903"
admin_token = "admin-token-for-local-dev"
```

**Step 2: Commit**

```bash
git add infrastructure/garage.toml
git commit -m "infra: add garage configuration"
```

---

### Task 2: Create Garage Init Script

**Files:**
- Create: `infrastructure/garage-init.sh`

**Step 1: Create init script**

```bash
#!/bin/bash
set -e

GARAGE_ADMIN="http://garage:3903"
BUCKET_NAME="buildkit-cache"
KEY_NAME="buildkit-key"
# Fixed credentials that match what we'll put in building.py
ACCESS_KEY_ID="GKbuildkit00000000000000000000000"
SECRET_KEY="buildkitsecret000000000000000000000000000000"

echo "Waiting for Garage admin API..."
until curl -sf "${GARAGE_ADMIN}/health" > /dev/null 2>&1; do
    sleep 1
done
echo "Garage is ready"

# Get cluster status and node ID
echo "Getting cluster status..."
CLUSTER_STATUS=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/status")
NODE_ID=$(echo "$CLUSTER_STATUS" | jq -r '.node')

# Check if layout already configured
LAYOUT=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/layout")
STAGED_COUNT=$(echo "$LAYOUT" | jq '.stagedRoleChanges | length')

if [ "$STAGED_COUNT" -eq 0 ]; then
    ROLES=$(echo "$LAYOUT" | jq '.roles | length')
    if [ "$ROLES" -eq 0 ]; then
        echo "Configuring node layout..."
        curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
            -H "Content-Type: application/json" \
            -d "{\"$NODE_ID\": {\"zone\": \"dc1\", \"capacity\": 1073741824, \"tags\": []}}" \
            "${GARAGE_ADMIN}/v1/layout"

        echo "Applying layout..."
        CURRENT_VERSION=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/layout" | jq '.version')
        curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
            -H "Content-Type: application/json" \
            -d "{\"version\": $((CURRENT_VERSION + 1))}" \
            "${GARAGE_ADMIN}/v1/layout/apply"
    fi
fi

# Check if bucket exists
echo "Checking bucket..."
BUCKETS=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/bucket?list")
BUCKET_EXISTS=$(echo "$BUCKETS" | jq -r ".[] | select(.globalAliases[]? == \"$BUCKET_NAME\") | .id")

if [ -z "$BUCKET_EXISTS" ]; then
    echo "Creating bucket ${BUCKET_NAME}..."
    BUCKET_RESULT=$(curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
        -H "Content-Type: application/json" \
        -d "{\"globalAlias\": \"$BUCKET_NAME\"}" \
        "${GARAGE_ADMIN}/v1/bucket")
    BUCKET_ID=$(echo "$BUCKET_RESULT" | jq -r '.id')
else
    BUCKET_ID="$BUCKET_EXISTS"
fi
echo "Bucket ID: $BUCKET_ID"

# Check if key exists
echo "Checking access key..."
KEYS=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/key?list")
KEY_EXISTS=$(echo "$KEYS" | jq -r ".[] | select(.name == \"$KEY_NAME\") | .id")

if [ -z "$KEY_EXISTS" ]; then
    echo "Creating access key ${KEY_NAME}..."
    curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"$KEY_NAME\", \"accessKeyId\": \"$ACCESS_KEY_ID\", \"secretAccessKey\": \"$SECRET_KEY\"}" \
        "${GARAGE_ADMIN}/v1/key/import"
fi

# Grant bucket permissions to key
echo "Granting bucket permissions..."
curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
    -H "Content-Type: application/json" \
    -d "{\"bucketId\": \"$BUCKET_ID\", \"accessKeyId\": \"$ACCESS_KEY_ID\", \"permissions\": {\"read\": true, \"write\": true, \"owner\": true}}" \
    "${GARAGE_ADMIN}/v1/bucket/allow"

echo "Garage initialization complete!"
echo "  Bucket: $BUCKET_NAME"
echo "  Access Key ID: $ACCESS_KEY_ID"
```

**Step 2: Make executable and commit**

```bash
chmod +x infrastructure/garage-init.sh
git add infrastructure/garage-init.sh
git commit -m "infra: add garage initialization script"
```

---

### Task 3: Create Docker Compose File

**Files:**
- Create: `docker-compose.yml`

**Step 1: Create docker-compose.yml**

```yaml
services:
  garage:
    image: dxflrs/garage:v2.1.0
    ports:
      - "127.0.0.1:3900:3900"  # S3 API
      - "127.0.0.1:3901:3901"  # RPC
      - "127.0.0.1:3903:3903"  # Admin
    volumes:
      - garage-meta:/var/lib/garage/meta
      - garage-data:/var/lib/garage/data
      - ./infrastructure/garage.toml:/etc/garage.toml:ro
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:3903/health"]
      interval: 5s
      timeout: 5s
      retries: 10

  garage-init:
    image: curlimages/curl:latest
    depends_on:
      garage:
        condition: service_healthy
    volumes:
      - ./infrastructure/garage-init.sh:/init.sh:ro
    entrypoint: ["/bin/sh", "/init.sh"]
    restart: "no"

  registry:
    image: registry:2
    ports:
      - "127.0.0.1:5050:5000"
    volumes:
      - registry-data:/var/lib/registry
    environment:
      REGISTRY_STORAGE_DELETE_ENABLED: "true"

  registry-ui:
    image: joxit/docker-registry-ui:latest
    ports:
      - "127.0.0.1:5051:80"
    environment:
      - REGISTRY_TITLE=Image Manager Registry
      - NGINX_PROXY_PASS_URL=http://registry:5000
      - SINGLE_REGISTRY=true
    depends_on:
      - registry

volumes:
  garage-meta:
  garage-data:
  registry-data:
```

**Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "infra: add docker-compose for garage, registry, and registry-ui"
```

---

### Task 4: Update building.py - Remove Registry Management

**Files:**
- Modify: `manager/building.py`

**Step 1: Remove registry management functions**

Remove these functions entirely:
- `is_registry_running()` (lines ~337-346)
- `start_registry()` (lines ~349-386)
- `stop_registry()` (lines ~389-401)
- `ensure_registry()` (lines ~404-408)

**Step 2: Add connection check function**

Add after `get_registry_addr_for_buildkit()`:

```python
def check_registry_connection() -> bool:
    """Check if registry is reachable."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', REGISTRY_PORT))
        sock.close()
        return result == 0
    except Exception:
        return False
```

**Step 3: Commit**

```bash
git add manager/building.py
git commit -m "refactor(building): remove registry container management"
```

---

### Task 5: Update building.py - Remove Garage Management

**Files:**
- Modify: `manager/building.py`

**Step 1: Remove garage management functions**

Remove these functions entirely:
- `get_garage_config_dir()` (lines ~413-415)
- `get_garage_credentials_file()` (lines ~418-420)
- `get_garage_credentials()` (lines ~451-461)
- `save_garage_credentials()` (lines ~464-472)
- `generate_garage_config()` (lines ~475-502)
- `start_garage()` (lines ~505-568)
- `_initialize_garage_cluster()` (lines ~571-691)
- `stop_garage()` (lines ~694-706)
- `ensure_garage()` (lines ~709-713)
- `is_garage_running()` (lines ~439-448)

**Step 2: Update credential constants to fixed values**

Replace the existing placeholder constants:

```python
# Garage for S3-compatible build cache
GARAGE_S3_PORT = 3900
GARAGE_RPC_PORT = 3901
GARAGE_ADMIN_PORT = 3903
GARAGE_BUCKET = "buildkit-cache"
GARAGE_REGION = "garage"
# Fixed credentials matching infrastructure/garage-init.sh
GARAGE_ACCESS_KEY_ID = "GKbuildkit00000000000000000000000"
GARAGE_SECRET_KEY = "buildkitsecret000000000000000000000000000000"
```

**Step 3: Add garage connection check**

```python
def check_garage_connection() -> bool:
    """Check if garage S3 endpoint is reachable."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', GARAGE_S3_PORT))
        sock.close()
        return result == 0
    except Exception:
        return False
```

**Step 4: Commit**

```bash
git add manager/building.py
git commit -m "refactor(building): remove garage container management, use fixed credentials"
```

---

### Task 6: Update building.py - Remove auto_start Parameters

**Files:**
- Modify: `manager/building.py`

**Step 1: Update build_image function**

Remove `auto_start` parameter from `build_image()`. Replace the auto-start logic with connection checks:

```python
def build_image(
    image_ref: str,
    context_path: Path | None = None,
    use_cache: bool = True,
    snapshot_id: str | None = None,
    plat: str | None = None,
) -> int:
```

At the start of the function, replace:
```python
if auto_start:
    if not ensure_buildkitd():
        ...
    if not ensure_registry():
        ...
    if use_cache and not ensure_garage():
        ...
```

With:
```python
if not check_registry_connection():
    print("Error: Registry not reachable at localhost:5050", file=sys.stderr)
    print("Run 'docker compose up -d' to start infrastructure services.", file=sys.stderr)
    return 1

if use_cache and not check_garage_connection():
    print("Warning: Garage not reachable, building without cache", file=sys.stderr)
    use_cache = False
```

**Step 2: Update create_manifest_from_registry**

Remove `auto_start` parameter. Replace auto-start logic with:

```python
def create_manifest_from_registry(
    image_ref: str,
    snapshot_id: str | None = None,
) -> int:
```

Replace the auto-start check with:
```python
if not check_registry_connection():
    print("Error: Registry not reachable at localhost:5050", file=sys.stderr)
    print("Run 'docker compose up -d' to start infrastructure services.", file=sys.stderr)
    return 1
```

**Step 3: Commit**

```bash
git add manager/building.py
git commit -m "refactor(building): remove auto_start, add connection checks"
```

---

### Task 7: Update __main__.py - Remove Registry/Garage from CLI

**Files:**
- Modify: `manager/__main__.py`

**Step 1: Update imports in cmd_build**

Change line 231:
```python
from manager.building import run_build, ensure_buildkitd
```

Remove `ensure_registry, ensure_garage` from import.

**Step 2: Update cmd_build function**

Replace lines 273-282:
```python
# Start buildkitd (still Python-managed)
if not ensure_buildkitd():
    print("Error: Failed to start buildkitd", file=sys.stderr)
    return 1
```

Remove the ensure_registry and ensure_garage calls - building.py handles connection checks now.

**Step 3: Update imports in cmd_manifest**

Change line 308:
```python
from manager.building import create_manifest_from_registry
```

Remove `ensure_registry` from import.

**Step 4: Update cmd_manifest function**

Remove lines 331-334 (the ensure_registry block).

**Step 5: Update cmd_start**

Change line 492:
```python
from manager.building import start_buildkitd
from manager.testing import start_dind
```

Update valid_daemons on line 496:
```python
valid_daemons = ("all", "buildkitd", "dind")
```

Remove lines 508-516 (the registry and garage start blocks).

**Step 6: Update cmd_stop**

Change line 528:
```python
from manager.building import stop_buildkitd
from manager.testing import stop_dind
```

Update valid_daemons:
```python
valid_daemons = ("all", "buildkitd", "dind")
```

Remove lines 542-546 (the registry and garage stop blocks).

**Step 7: Update cmd_status**

Change line 556:
```python
from manager.building import is_buildkitd_running, get_socket_addr, check_registry_connection, get_registry_addr, check_garage_connection, get_garage_s3_endpoint
from manager.testing import is_dind_running, get_docker_host
```

Update valid_daemons:
```python
valid_daemons = ("all", "buildkitd", "dind")
```

Remove registry and garage status blocks (lines 575-587).

**Step 8: Update print_usage**

Change line 24:
```python
print("  start [daemon]      Start daemons (buildkitd, dind, or all)")
print("  stop [daemon]       Stop daemons (buildkitd, dind, or all)")
```

**Step 9: Commit**

```bash
git add manager/__main__.py
git commit -m "refactor(cli): remove registry/garage from start/stop/status commands"
```

---

### Task 8: Update README.md

**Files:**
- Modify: `README.md`

**Step 1: Update daemon management section**

Find the "### Daemon management" section and update to:

```markdown
### Infrastructure setup

Start the infrastructure services (registry, cache):

```shell
docker compose up -d
```

View registry UI: http://localhost:5051

Stop infrastructure:
```shell
docker compose down        # Keep data
docker compose down -v     # Delete data
```

### Daemon management

```shell
uv run image-manager start             # Start buildkitd + dind
uv run image-manager start buildkitd   # Start only buildkitd
uv run image-manager start dind        # Start only dind
uv run image-manager status            # Check status
uv run image-manager stop              # Stop all
```

**buildkitd** (for building):
- **Linux**: Runs natively using the bundled binary
- **macOS**: Runs rootless in a Docker container (`moby/buildkit:rootless`)

**dind** (for testing):
- **Linux**: Runs with minimal capabilities (`SYS_ADMIN`, `NET_ADMIN`, `MKNOD`)
- **macOS**: Runs in Docker Desktop (requires privileged due to VM cgroup limitations)
- Images are loaded from tar archives into the isolated daemon
```

**Step 2: Update architecture diagram**

Update the "Local development" diagram:

```markdown
```
┌─────────────────────────────────────────────────────────────────────────┐
│                             Host Machine                                │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    docker compose up -d                          │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │   │
│  │  │    garage    │  │   registry   │  │  registry-ui │          │   │
│  │  │   (S3 cache) │  │ (registry:2) │  │   (joxit)    │          │   │
│  │  │   :3900      │  │   :5050      │  │   :5051      │          │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘          │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐                                    │
│  │  buildkitd   │  │     dind     │   ← image-manager start            │
│  │  (rootless)  │  │  (testing)   │                                    │
│  │  :8372       │  │  :2375       │                                    │
│  └──────────────┘  └──────────────┘                                    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```
```

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: update for docker compose infrastructure"
```

---

### Task 9: Test the Setup

**Step 1: Start compose services**

```bash
docker compose up -d
```

Expected: All services start, garage-init runs successfully.

**Step 2: Check services are healthy**

```bash
docker compose ps
```

Expected: garage, registry, registry-ui running. garage-init exited 0.

**Step 3: Open registry UI**

```bash
open http://localhost:5051
```

Expected: Registry UI loads showing empty registry.

**Step 4: Run a build to verify integration**

```bash
uv run image-manager generate
uv run image-manager build base:2025.09 --platform amd64
```

Expected: Build succeeds, uses S3 cache, pushes to registry.

**Step 5: Verify image in registry UI**

Open http://localhost:5051 and verify base image appears.

**Step 6: Commit any fixes if needed**

---

### Task 10: Clean Up Old Garage Data Directory

**Files:**
- Modify: `.gitignore` (if needed)

**Step 1: Document cleanup in README**

Add to README Quick Start section:

```markdown
## Quick Start

1. Start infrastructure:
   ```bash
   docker compose up -d
   ```

2. Start build daemons:
   ```bash
   uv run image-manager start
   ```

3. Generate and build:
   ```bash
   uv run image-manager generate
   uv run image-manager build
   ```

**Note:** If you previously used the Python-managed garage, you can remove old data:
```bash
rm -rf ~/.cache/image-manager/garage
```
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add quick start and cleanup instructions"
```
