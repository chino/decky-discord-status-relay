# Discord Status Relay (Decky plugin)

Shows the game you're playing in Game Mode as your Discord status, with the game's
official name and icon when Discord knows it. Discord can run **on the handheld** or
on **another machine**, such as your desktop PC, so the handheld doesn't have to run
Discord at all.

## Quick setup

1. **On the handheld:** install the plugin (until it is in the Decky store: Decky settings, Developer, *Install plugin from URL*, using a release zip), open it from the quick-access menu, and set
   **Target** to your PC's address (its LAN IP, hostname or Tailscale name). That's it if
   Discord runs on the handheld instead: leave Target empty.
2. **On the machine running Discord**, start a receiver:
   - **Windows**, in an admin PowerShell (installs it to start with Windows and opens the
     firewall for your LAN and Tailscale):
     ```powershell
     & ([scriptblock]::Create((irm https://raw.githubusercontent.com/chino/decky-discord-status-relay/main/receivers/windows/install.ps1)))
     ```
   - **Linux**, quick and temporary (no install, stops when you close the terminal):
     ```bash
     socat TCP-LISTEN:8920,fork,reuseaddr UNIX-CONNECT:$XDG_RUNTIME_DIR/discord-ipc-0
     ```
     or with the advanced receiver straight from GitHub:
     ```bash
     curl -fsSL https://raw.githubusercontent.com/chino/decky-discord-status-relay/main/receivers/python/discord-receiver.py | python3 -
     ```
3. Start a game. Your Discord profile shows it within a few seconds.

On a home LAN or your own tailnet, that's all you need. Tokens, TLS and permanent Linux
installs are covered below.

## What it does

- **Reports the running game**, including non-Steam shortcuts (Heroic, emulators, ...).
  Discord shows "Playing <game>" using the game's own Discord application when one exists,
  otherwise a fallback application with the game name underneath.
- **Owns the Discord socket** (`$XDG_RUNTIME_DIR/discord-ipc-0`). Native Linux games with
  their own rich presence connect to the plugin, which passes them through.
- **Avoids double statuses:** while a game shows its own rich presence, the plugin
  withholds its generic one and puts it back when the game's session ends.
- **Routes to a Discord:**
  - *Automatic* (default): the Discord app on this device if it's running, otherwise the
    configured host.
  - *This device only* or *Another machine only*.
- **Survives drops:** if Discord or the network goes away, clients stay connected; the plugin
  answers them itself, reconnects (backoff up to 10 s), and replays the handshake and last status.
  Remote links use TCP keepalive, so a machine that vanishes is noticed in about 30 s.
- **Switches live:** in Automatic mode, starting Discord on the handheld moves statuses to it
  within a few seconds, and quitting it moves them back to the target.
- **Re-asserts the status** every 15 s, and retries after 5 s on failure.
- **Optional shared token** so the receiver only accepts your handheld.

Settings live in the quick-access panel: destination, target, token, certificate fingerprint, device name
("on ROG Ally" instead of "on Steam Deck"), plus a live relay status line.

## Target

One field says where statuses go. The format picks the transport:

| Target | Transport |
|---|---|
| `pc.lan`, `100.64.0.5` | plain TCP on the default port **8920** |
| `pc.lan:9000`, `tcp://pc.lan:9000` | plain TCP |
| `tls://pc.lan:8921` | TLS |
| `http://pc.lan:8922/discord` | WebSocket over HTTP (`ws://` also accepted) |
| `https://pc.example.ts.net/discord` | WebSocket over HTTPS (`wss://` also accepted) |

The optional **token** is sent as a first line `AUTH <token>` on TCP/TLS, or as an
`Authorization: Bearer <token>` header over HTTP(S). For a self-signed certificate, paste its
SHA-256 **fingerprint** into the plugin to pin it; otherwise certificates are verified normally.

## Receivers (for Discord on another machine)

A receiver runs on the machine with Discord, accepts the plugin's connection and passes it
into the local Discord.

| | Basic: `receivers/windows/discord-receiver.ps1` | Advanced: `receivers/python/discord-receiver.py` |
|---|---|---|
| Runs on | Windows (PowerShell 5.1, built in) | Linux, macOS, Windows (Windows untested) |
| Install | nothing | Python 3.8+, standard library only |
| Listeners | plain TCP | `tcp://`, `tls://`, `http://` (WebSocket), `https://` (WebSocket), several at once |
| TLS | via a proxy in front (see below) | built in: `--cert/--key`, or `--self-signed DIR` (prints the fingerprint) |
| Token | `-Token` | `--token` or `DISCORD_RECEIVER_TOKEN` |
| Address allow-list | `-Allow` (optional) | `--allow` (optional) |
| Health check | | plain `GET` on the HTTP path returns 200 |
| Runs as | Task Scheduler task for your user (`install.ps1`): at sign-in, re-checked every 5 min, no window, single instance | systemd user unit (`discord-receiver.service`), or by hand |

Basic, on Windows:

```powershell
# once, elevated: allow the handheld in
New-NetFirewallRule -DisplayName 'Discord Status Relay receiver' -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8920 -RemoteAddress 192.0.2.20
# as yourself
powershell -ExecutionPolicy Bypass -File receivers\windows\install.ps1 -Allow 192.0.2.20 -Token <secret>
```

Advanced, on Linux (self-signed TLS plus a token):

```bash
install -Dm755 receivers/python/discord-receiver.py ~/.local/bin/discord-receiver.py
install -Dm644 receivers/python/discord-receiver.service ~/.config/systemd/user/discord-receiver.service
systemctl --user edit discord-receiver
#   [Service]
#   Environment=DISCORD_RECEIVER_TOKEN=<secret>
#   Environment="RECEIVER_ARGS=--listen tls://0.0.0.0:8921 --self-signed %h/.config/discord-receiver"
systemctl --user enable --now discord-receiver
journalctl --user -u discord-receiver | grep fingerprint   # paste into the plugin
```

Quick test without either: `socat TCP-LISTEN:8920,fork,range=192.0.2.20/32 UNIX-CONNECT:$XDG_RUNTIME_DIR/discord-ipc-0`
(Linux, no token).

## Security and TLS

Authentication is optional. On a home LAN or a tailnet only you use, most people skip it.

- **Tailscale and WireGuard already encrypt everything** between your devices, so plain `tcp://`
  to a tailnet address is private, token included.
- **On any other network, a token alone isn't enough:** over plain `tcp://` or `http://` it
  travels in the clear and anyone watching the network can read it. Add encryption:
  - *Advanced receiver, self-signed:* `--listen tls://0.0.0.0:8921 --self-signed DIR`, then target
    `tls://pc:8921` with the printed fingerprint pinned in the plugin.
  - *Real certificate from Tailscale* (`tailscale serve` is tailnet-only; `tailscale funnel` would
    publish it to the internet). In front of the advanced receiver's HTTP listener:
    `tailscale serve --bg --https=8443 http://127.0.0.1:8922` and target
    `https://pc.<tailnet>.ts.net:8443/discord`. In front of the basic receiver's raw TCP:
    `tailscale serve --bg --tls-terminated-tcp=8443 tcp://127.0.0.1:8920` and target
    `tls://pc.<tailnet>.ts.net:8443`. Bind the receiver to `127.0.0.1` so only the proxy reaches it.
  - *Any reverse proxy* (Caddy, nginx, Traefik) that can pass WebSockets works the same way with
    the HTTP listener.
- **Only use Funnel or a public port with a token**, and prefer TLS.
- If verifying a real certificate fails on the handheld (some Python builds lack a CA bundle), pin
  its fingerprint instead.

### Protocol

Discord IPC frames (8-byte little-endian header with opcode and length, then JSON) carried
unchanged: as a raw byte stream on TCP/TLS, or in binary WebSocket messages over HTTP(S).

## Windows games under Proton

A Proton game's Discord library writes to a Windows named pipe inside its Wine prefix, which
never reaches the plugin's socket. With **Rich presence from Proton games** on (the default),
the plugin ships [rpc-bridge](https://github.com/EnderIce2/rpc-bridge) (MIT) and starts it in
every Proton prefix, so a game's own status (map, party, timers) shows up instead of just
"Playing <game>". No per-game launch options.

How: the plugin writes `~/.config/environment.d/50-discord-status-relay-proton.conf`, which
sets `PROTON_REMOTE_DEBUG_CMD` for the whole Steam session. Steam reads it at start, so
**restart Game Mode** after turning the option on or off; the panel says when that's needed.
Turning it off or uninstalling removes the file. `bridge.exe` is built from source in
`backend/` with one patch for Proton 11 (see `backend/README.md`).

## Development

```bash
pnpm install && pnpm build          # frontend -> dist/
python3 -u tests/test_hub.py        # hub + receiver tests with a fake Discord (needs openssl)
python3 -u tests/test_proton_bridge.py   # Proton environment file
docker build -t dp-backend backend && docker run --rm -v "$PWD/backend:/backend" dp-backend   # bridge.exe
node scripts/deploy.mjs --zip --no-deploy   # package to out/
```

The backend is `main.py` plus `py_modules/hub.py`. The hub has no Decky imports, so it's
tested on its own.

## Notes

- Discord silently ignores commands sent before it answers the handshake (READY).
- The "Playing ___" line is the Discord application's name; Discord doesn't match by text.
  Games Discord detects on PC have public application IDs, which the plugin reuses.

## License

BSD 3-Clause, see `LICENSE`.
