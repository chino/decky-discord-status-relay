# backend: rpc-bridge for Proton games

Windows games under Proton talk to Discord through a named pipe inside their Wine
prefix, which never reaches the Linux socket the hub listens on.
[rpc-bridge](https://github.com/EnderIce2/rpc-bridge) (MIT) runs inside the prefix and
copies that pipe to the hub's socket.

- `entrypoint.sh` builds `bridge.exe` from a pinned commit of
  [chino/rpc-bridge](https://github.com/chino/rpc-bridge): upstream v1.4.1.3 plus one fix.
  Upstream exits on any non-Staging Wine without AF_UNIX support, which includes Proton 11
  (Wine 11.0), although its syscall fallback works there. The fix keeps that check for
  Wine 11.5 and newer only, where the fallback really can't work. Sent upstream; once it is
  merged, point `REPO`/`COMMIT` back at EnderIce2/rpc-bridge.

Build: `docker build -t dp-backend backend && docker run --rm -v "$PWD/backend:/backend" dp-backend`
(the decky CLI does the same). Output: `backend/out/bridge.exe` and `LICENSE.rpc-bridge`,
shipped in the plugin's `bin/`.
