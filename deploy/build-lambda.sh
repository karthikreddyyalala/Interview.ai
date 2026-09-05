#!/usr/bin/env bash
#
# Builds the Lambda deployment zip with Linux-correct wheels. pydantic_core is
# a compiled extension, so we fetch manylinux x86_64 wheels for the Lambda
# python3.12 runtime rather than the host's macOS wheels.
# Output: deploy/build/crucible-api.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/deploy/build"
PKG="$BUILD/pkg"

rm -rf "$BUILD"
mkdir -p "$PKG"

echo "==> Fetching Linux (manylinux2014 x86_64) deps for python3.12"
python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --implementation cp \
  --only-binary=:all: \
  --no-cache-dir \
  --target "$PKG" \
  -r "$ROOT/deploy/requirements-lambda.txt"

echo "==> Copying backend source into the package"
cd "$ROOT/backend"
for item in app.py lambda_handler.py auth.py config models agents graph llm routes store prompts data; do
  cp -R "$item" "$PKG/"
done

echo "==> Pruning caches"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> Zipping"
cd "$PKG"
zip -qr "$BUILD/crucible-api.zip" . -x "*.pyc"
cd "$ROOT"
SIZE=$(du -h "$BUILD/crucible-api.zip" | cut -f1)
echo "Built deploy/build/crucible-api.zip ($SIZE)"
