# install-agent

A CLI tool to install agent Helm charts into Kubernetes clusters. This tool is part of the AgentCert agent-charts repository and is used to deploy agent charts (e.g., `flash-agent`) packaged within its Docker image.

## Features

- **Idempotent installs** via `helm upgrade --install` (default)
- **Robust namespace handling** — pre-creates namespace with Helm ownership metadata
- **Helm v3.14 rate limiter workaround** — uses `kubectl rollout status` instead of `helm --wait`
- **Configurable** — supports custom values files, set overrides, dry-run, timeouts

## Usage

```bash
# Install flash-agent into its own namespace
install-agent -folder flash-agent -namespace flash-agent

# Install with custom values
install-agent -folder flash-agent -values /custom/values.yaml

# Dry-run
install-agent -folder flash-agent -dry-run
```

## Build

```bash
# From the install-agent directory
make build

# Build and push to Docker Hub
make build-push
```

## Docker Image

The Docker image bundles:
- The `install-agent` Go binary
- All charts from `charts/` directory
- Helm v3.14.0
- kubectl v1.29.0
