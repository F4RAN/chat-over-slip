#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker run --rm \
  --platform linux/amd64 \
  -v "$ROOT_DIR":/work \
  -w /work \
  python:3.9-slim \
  bash -lc '
    set -euo pipefail
    export PYINSTALLER_CONFIG_DIR=/work/.pyinstaller-linux
    apt-get update
    apt-get install -y --no-install-recommends gcc
    python -m pip install --upgrade pip
    python -m pip install -r build/requirements-build.txt
    pyinstaller --noconfirm --clean build/chat_over_dnstt.spec
    cp dist/chat-over-dnstt dist/chat-over-dnstt-linux-x86_64
  '

echo
echo "Linux x86_64 executable:"
echo "  $ROOT_DIR/dist/chat-over-dnstt-linux-x86_64"
