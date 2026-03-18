# Chat over DNSTT - Electron GUI

Electron-based chat client with:
- Persian/RTL support
- Message bubbles
- Sound notifications
- Same SSH/DNS transport as the TUI

## Run

```bash
npm install
npm start
```

## Build

```bash
npm run build        # all platforms
npm run build:mac    # macOS
npm run build:linux  # Linux
```

## Config

On first launch, enter host/domain, user, password, and mode (ssh or dns). For DNS mode, ensure slipstream clients are running before connecting (e.g. start them from the Python launcher first).
