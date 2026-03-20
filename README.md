# chat-over-slip

Terminal and desktop chat over **SSH** or **DNS tunneling** (dnstt / [Slipstream](https://github.com/Mygod/slipstream-rust)). This repo is the chat client/launcher and server-side [`chat.sh`](chat.sh) log; the tunnel itself is provided by Slipstream (Rust) or your existing dnstt stack.

---

## This repo (quick)

**Chat server:** clone on the host, copy [`.env.example`](.env.example) → `.env` if you use Telegram helpers (`tg_*.py`), and use [`chat.sh`](chat.sh) as the shared message backend from the directory your clients expect.

**Chat client:** prebuilt GUI/TUI + [`install.sh`](install.sh) on [Releases](https://github.com/F4RAN/chat-over-slip/releases), or run from source (`chat_tui`, `desktop_ui`, `launcher`).

---

## Slipstream tunnel (server + client)

Slipstream carries QUIC inside DNS. Full detail lives upstream: **[Mygod/slipstream-rust](https://github.com/Mygod/slipstream-rust)** ([docs index](https://github.com/Mygod/slipstream-rust/blob/main/docs/README.md), [usage](https://github.com/Mygod/slipstream-rust/blob/main/docs/usage.md), [build](https://github.com/Mygod/slipstream-rust/blob/main/docs/build.md)).

### Prerequisites (building Slipstream from source)

- Rust (stable), `cmake`, `pkg-config`, OpenSSL headers/libs, `python3` (interop/scripts)
- Initialize the picoquic submodule:  
  `git submodule update --init --recursive`
- First `cargo build` can auto-build picoquic via `./scripts/build_picoquic.sh` (see upstream `docs/build.md`). Set `PICOQUIC_AUTO_BUILD=0` to disable.

### Build binaries

```bash
cargo build -p slipstream-client -p slipstream-server
```

### TLS cert (optional)

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout key.pem -out cert.pem -days 365 \
  -subj "/CN=slipstream"
```

If the paths you pass to the server do not exist, the server can auto-generate a self-signed ECDSA cert (see upstream README). Use a **persistent** `--reset-seed` path so stateless reset tokens survive restarts.

### Run the **server**

```bash
cargo run -p slipstream-server -- \
  --dns-listen-port 8853 \
  --target-address 127.0.0.1:22 \
  --domain example.com \
  --cert ./cert.pem \
  --key ./key.pem \
  --reset-seed ./reset-seed
```

Point `--target-address` at whatever should receive the forwarded TCP flow (e.g. local SSH). Adjust `--domain` and DNS so queries reach this listener (port **53** in production usually requires root or capabilities).

### Run the **client**

```bash
cargo run -p slipstream-client -- \
  --tcp-listen-port 7000 \
  --resolver 203.0.113.53:53 \
  --domain example.com
```

You can use a resolver that forwards to your Slipstream server. Then point this repo’s SSH/DNS mode at `127.0.0.1:7000` (or the port you chose) as documented in your client config.

### Production: conntrack (UDP / DNS on port 53)

On a public server handling many DNS flows, raise conntrack limits above typical defaults. Upstream suggests a baseline such as:

| Setting | Value |
|--------|--------|
| `net.netfilter.nf_conntrack_max` | `262144` |
| `net.netfilter.nf_conntrack_udp_timeout` | `15` |
| `net.netfilter.nf_conntrack_udp_timeout_stream` | `60` |

Rough sizing: ~131072 entries per 1 GiB RAM, ~262144 for 2–4 GiB, ~524288 for 8 GiB+. Keep steady-state `conntrack -C` well under ~60% of `nf_conntrack_max`.

---

## Bundled client binary

This repository may include a prebuilt [`slipstream/slipstream-client`](slipstream/slipstream-client) (and release zips may ship platform-named copies). Prefer matching the version/build against your server; when in doubt, build both from the same [slipstream-rust](https://github.com/Mygod/slipstream-rust) revision.

## License

[Slipstream (Rust)](https://github.com/Mygod/slipstream-rust) is **Apache-2.0**. Add a `LICENSE` file to this repo when you publish it if you want an explicit license for the chat/launcher code here.
