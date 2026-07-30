#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# build_layers.sh — Pre-build Lambda layers for CDK deployment.
#
# Builds layers targeting Linux x86_64 (Lambda runtime).
# Run this BEFORE `cdk deploy` to avoid Docker/Finch dependency during synth.
#
# Usage:
#   bash scripts/build_layers.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
BUILD_DIR="${ROOT_DIR}/build"

echo "→ Building Lambda layers for Linux x86_64..."

# Clean
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/query-layer/python"
mkdir -p "$BUILD_DIR/interceptor-layer/python"

# Databricks Query Lambda layer
echo "  [1/2] Query layer (requests)..."
pip install -q \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --no-compile \
  --target "$BUILD_DIR/query-layer/python" \
  requests urllib3 certifi charset-normalizer idna 2>/dev/null
echo "    ✓ $(du -sh "$BUILD_DIR/query-layer" | awk '{print $1}')"

# Interceptor Lambda layer
echo "  [2/2] Interceptor layer (PyJWT + requests)..."
pip install -q \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --no-compile \
  --target "$BUILD_DIR/interceptor-layer/python" \
  PyJWT requests urllib3 certifi charset-normalizer idna 2>/dev/null
echo "    ✓ $(du -sh "$BUILD_DIR/interceptor-layer" | awk '{print $1}')"

echo ""
echo "  ✅ Layers built in: ${BUILD_DIR}/"
echo "     Run: cd cdk && cdk deploy --all"
