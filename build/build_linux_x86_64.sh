#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker run --rm \
  --platform linux/amd64 \
  -v "$ROOT_DIR":/work \
  -w /work \
  python:3.11-slim \
  bash -lc '
    set -euo pipefail
    export PYINSTALLER_CONFIG_DIR=/work/.pyinstaller-linux
    apt-get update
    apt-get install -y --no-install-recommends \
      gcc \
      libegl1 libgl1-mesa-glx libglib2.0-0 libfontconfig1 \
      libxkbcommon0 libdbus-1-3 libxcb-cursor0 libxcb-icccm4 \
      libxcb-keysyms1 libxcb-shape0 libxcb-xkb1 libxcb-render-util0 \
      libxcb-xinerama0 libxcb-randr0 libxcb-image0
    python -m pip install --upgrade pip
    python -m pip install -r build/requirements-build.txt
    export QT_QPA_PLATFORM=offscreen
    pyinstaller --noconfirm --clean build/chat_over_dnstt.spec
    cp dist/chat-over-dnstt dist/chat-over-dnstt-gui-linux-x86_64
    pyinstaller --noconfirm --clean build/chat_over_dnstt_tui.spec
    cp dist/chat-over-dnstt-tui dist/chat-over-dnstt-tui-linux-x86_64
  '

echo
echo "Linux x86_64 executables:"
echo "  GUI: $ROOT_DIR/dist/chat-over-dnstt-gui-linux-x86_64"
echo "  TUI: $ROOT_DIR/dist/chat-over-dnstt-tui-linux-x86_64"
