#!/usr/bin/env bash
# Build the Anthropic Lambda Layer cross-compiled for Amazon Linux 2 (x86_64).
# Must be run before `cdk deploy` in CI or locally whenever the SDK version changes.
#
# Usage:
#   ./scripts/build_layer.sh          # builds into lambda/anthropic_layer/
#   ./scripts/build_layer.sh --clean  # wipes and rebuilds

set -euo pipefail

LAYER_DIR="lambda/anthropic_layer/python"
SDK_VERSION="anthropic>=0.49.0,<1.0.0"

if [[ "${1:-}" == "--clean" ]]; then
  echo "→ Cleaning $LAYER_DIR"
  rm -rf lambda/anthropic_layer
fi

if [[ -d "$LAYER_DIR" ]]; then
  echo "→ Layer already built at $LAYER_DIR (pass --clean to rebuild)"
  exit 0
fi

echo "→ Building Anthropic SDK layer into $LAYER_DIR"
mkdir -p "$LAYER_DIR"

pip install \
  --quiet \
  --platform manylinux2014_x86_64 \
  --target "$LAYER_DIR" \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  "$SDK_VERSION"

echo "✓ Layer built: $(du -sh $LAYER_DIR | cut -f1)"
