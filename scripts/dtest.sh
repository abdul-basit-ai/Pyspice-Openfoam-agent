#!/usr/bin/env bash
# dtest.sh — run the full test suite inside the Phase 0 container.
#
# The canonical execution environment is the docker-agent image (Phase 0
# decision): ngspice and OpenFOAM only exist there, so simulation-dependent
# tests (the ones skipped on the Windows host) run here.
#
# Usage:
#   bash scripts/dtest.sh            # full suite (incl. ngspice tests)
#   bash scripts/dtest.sh -k sizing  # forwarded to pytest
#
# Requires: docker with the built image (docker compose -f docker/docker-compose.yml build)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${DTEST_IMAGE:-docker-agent:latest}"

# Rebuild the image if it doesn't exist yet
if ! docker image inspect "${IMAGE}" > /dev/null 2>&1; then
    echo "Image ${IMAGE} not found — building it first (one-time, ~5-10 min)..."
    docker build -f docker/Dockerfile -t "${IMAGE}" .
fi

# Mount the repo read-write so pytest runs against the live working tree
exec docker run --rm \
    -v "$(cd "${REPO_ROOT}" && pwd):/workspace" \
    -w /workspace \
    --entrypoint /bin/bash \
    "${IMAGE}" \
    -c "pip3 install -e . -q 2>/dev/null; python3 -m pytest tests/ -v ${*:-}"
