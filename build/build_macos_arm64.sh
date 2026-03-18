#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv-build-macos"
export PYINSTALLER_CONFIG_DIR="$ROOT_DIR/.pyinstaller-macos"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/build/requirements-build.txt"
"$VENV_DIR/bin/pyinstaller" --noconfirm --clean "$ROOT_DIR/build/chat_over_dnstt.spec"
mv "$ROOT_DIR/dist/chat-over-dnstt" "$ROOT_DIR/dist/chat-over-dnstt-gui-macos-arm64"

"$VENV_DIR/bin/pyinstaller" --noconfirm --clean "$ROOT_DIR/build/chat_over_dnstt_tui.spec"
mv "$ROOT_DIR/dist/chat-over-dnstt-tui" "$ROOT_DIR/dist/chat-over-dnstt-tui-macos-arm64"

echo
echo "macOS arm64 executables:"
echo "  GUI: $ROOT_DIR/dist/chat-over-dnstt-gui-macos-arm64"
echo "  TUI: $ROOT_DIR/dist/chat-over-dnstt-tui-macos-arm64"
