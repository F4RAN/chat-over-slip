#!/usr/bin/env bash
# Install chat-over-dnstt and optionally slipstream-client.
# Run from the directory containing the downloaded executables.
# Usage: sudo ./install.sh

set -e

# Re-run with sudo if not root
if [[ "$(id -u)" -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Detect platform
case "$(uname -s)" in
  Linux)
    BIN_DIR="/usr/local/bin"
    SLIP_DIR="/usr/bin"
    APP_DEST="$BIN_DIR/chat-over-dnstt"
    SLIP_DEST="$SLIP_DIR/slipstream-client"
    ;;
  Darwin)
    BIN_DIR="/usr/local/bin"
    APP_DEST="/Applications/chat-over-dnstt"
    SLIP_DEST="$BIN_DIR/slipstream-client"
    ;;
  *)
    echo "Unsupported OS: $(uname -s)"
    exit 1
    ;;
esac

# Find chat-over-dnstt executable
CHAT_EXE=""
for name in chat-over-dnstt-linux-x86_64 chat-over-dnstt-macos-arm64 chat-over-dnstt; do
  if [[ -f "$name" ]]; then
    CHAT_EXE="$name"
    break
  fi
done

if [[ -z "$CHAT_EXE" ]]; then
  echo "No chat-over-dnstt executable found in $SCRIPT_DIR"
  echo "Expected: chat-over-dnstt-linux-x86_64, chat-over-dnstt-macos-arm64, or chat-over-dnstt"
  exit 1
fi

# Install chat-over-dnstt (requires sudo for /usr/local/bin and /Applications)
chmod +x "$CHAT_EXE"
cp -f "$CHAT_EXE" "$APP_DEST"
chmod +x "$APP_DEST"
echo "Installed: $APP_DEST"

# Install slipstream-client if present
if [[ -f "slipstream-client" ]]; then
  chmod +x slipstream-client
  cp -f slipstream-client "$SLIP_DEST"
  chmod +x "$SLIP_DEST"
  echo "Installed: $SLIP_DEST"
else
  echo "slipstream-client not found in $SCRIPT_DIR (optional, skipped)"
fi

echo "Done. Run: $APP_DEST"
