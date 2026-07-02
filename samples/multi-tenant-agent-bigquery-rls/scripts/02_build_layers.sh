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
mkdir -p "$BUILD_DIR/bigquery-layer/python"
mkdir -p "$BUILD_DIR/interceptor-layer/python"

# BigQuery Query Lambda layer
echo "  [1/2] BigQuery layer (google-cloud-bigquery + google-auth)..."
pip install -q \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --no-compile \
  --target "$BUILD_DIR/bigquery-layer/python" \
  google-cloud-bigquery google-auth cffi cryptography 2>/dev/null
echo "    ✓ $(du -sh "$BUILD_DIR/bigquery-layer" | awk '{print $1}')"

# Interceptor Lambda layer
echo "  [2/2] Interceptor layer (google-auth + PyJWT + requests)..."
pip install -q \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --no-compile \
  --target "$BUILD_DIR/interceptor-layer/python" \
  google-auth PyJWT requests cffi cryptography 2>/dev/null
echo "    ✓ $(du -sh "$BUILD_DIR/interceptor-layer" | awk '{print $1}')"

echo ""
echo "  ✅ Layers built in: ${BUILD_DIR}/"
echo "     Run: cd cdk && cdk deploy --all"
