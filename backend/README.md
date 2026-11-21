# backend: rpc-bridge for Proton games

Windows games under Proton talk to Discord through a named pipe inside their Wine
prefix, which never reaches the Linux socket the hub listens on.
[rpc-bridge](https://github.com/EnderIce2/rpc-bridge) (MIT) runs inside the prefix and
copies that pipe to the hub's socket.

- `entrypoint.sh` builds `bridge.exe` from a pinned upstream commit.
- `rpc-bridge-proton.patch` removes upstream's "Wine Staging only" check. Proton 11 is
  Wine 11.0, not Staging, and has no AF_UNIX support, but rpc-bridge's own syscall
  fallback works there; upstream exits before trying it.

Build: `docker build -t dp-backend backend && docker run --rm -v "$PWD/backend:/backend" dp-backend`
(the decky CLI does the same). Output: `backend/out/bridge.exe` and `LICENSE.rpc-bridge`,
shipped in the plugin's `bin/`.
