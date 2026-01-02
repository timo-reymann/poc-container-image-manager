# Custom Registry Configuration Design

## Goal

Allow specifying a custom registry URL and optional authentication via a `.image-manager.yml` config file in the project root.

## Config File Format

Location: `.image-manager.yml` in project root (no user-level fallback).

```yaml
# Simple - just URL (no auth)
registry:
  url: my-registry.example.com:5000

# With authentication
registry:
  url: my-registry.example.com:5000
  username: deploy-user
  password: ${REGISTRY_PASSWORD}

# Full env var references
registry:
  url: ${REGISTRY_URL}
  username: ${REGISTRY_USERNAME}
  password: ${REGISTRY_PASSWORD}
```

### Defaults

- When file doesn't exist: use `localhost:5050` with no auth
- When `registry` key is missing: use `localhost:5050` with no auth
- When env var in `${VAR}` is missing for URL: use `localhost:5050`
- When env var is missing for username/password: treat as no auth

### Environment Variable Interpolation

The `${VAR}` syntax expands at runtime. Supports mixed literal and env var values:
- `url: my-registry.com:5000` - literal value
- `url: ${REGISTRY_URL}` - env var reference
- `password: ${REGISTRY_PASSWORD}` - env var for secrets

## Code Structure

New module: `manager/config.py`

```python
def load_config() -> dict:
    """Load .image-manager.yml from project root, return empty dict if missing."""

def expand_env_vars(value: str) -> str:
    """Expand ${VAR} references in a string value."""

def get_registry_url() -> str:
    """Get registry URL from config or default to localhost:5050."""

def get_registry_auth() -> tuple[str, str] | None:
    """Get (username, password) if configured, None otherwise."""
```

Config is loaded once and cached for the command duration.

## Authentication Flow

1. Check if `get_registry_auth()` returns credentials
2. If yes, run `docker login <registry> -u <user> -p <password>` before build
3. Buildctl inherits Docker's credential store automatically
4. No explicit logout needed

Connection check (`check_registry_connection`) only verifies TCP reachability. Auth errors surface during push.

## Changes to building.py

- Replace hardcoded `localhost:5050` with `get_registry_url()`
- Call `docker login` with credentials before pushing if auth configured
- Update error messages to show configured registry URL
