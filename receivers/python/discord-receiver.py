#!/usr/bin/env python3
"""Discord Presence receiver (advanced): runs on the machine with Discord.

Accepts connections from the Decky plugin (or any relay) and passes each one into the
local Discord app. Python 3.8+, standard library only. Linux, macOS and Windows.

Listeners (repeat --listen for several):
  tcp://0.0.0.0:8920            plain TCP; token as a first line "AUTH <token>"
  tls://0.0.0.0:8921            TLS; same token line
  http://0.0.0.0:8922/discord   WebSocket; token as "Authorization: Bearer <token>"
  https://0.0.0.0:8923/discord  WebSocket over TLS
A plain GET on an HTTP listener's path answers 200, for health checks.

TLS: --cert/--key, or --self-signed DIR to create (with the openssl command) and reuse
a certificate there. The SHA-256 fingerprint is printed at start; paste it into the
plugin's "Certificate fingerprint" field to pin it.

Security: optional --token (or DISCORD_RECEIVER_TOKEN) and --allow. Without them, anyone
who can reach the port can set your Discord status, which is usually fine on a home LAN
or your own tailnet. Tokens sent over plain tcp:// or http:// are readable on the
network unless that network is encrypted (Tailscale, WireGuard); otherwise use TLS.

Discord is found the way clients find it: discord-ipc-0..9 under $XDG_RUNTIME_DIR,
$TMPDIR, /tmp and the Flatpak and Snap folders on Linux/macOS; the named pipe
\\\\.\\pipe\\discord-ipc-N on Windows (Windows support is untested).
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import urllib.parse

log = logging.getLogger("discord-receiver")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
IS_WINDOWS = sys.platform == "win32"


# ----- finding Discord -----

def unix_candidates():
    bases = [os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp"]
    roots = []
    for b in filter(None, bases):
        roots += [b, os.path.join(b, "app/com.discordapp.Discord"), os.path.join(b, "snap.discord")]
    for root in dict.fromkeys(roots):
        for i in range(10):
            yield os.path.join(root, f"discord-ipc-{i}")


async def open_discord():
    """Returns (reader, writer, name) for the local Discord, or raises OSError."""
    if IS_WINDOWS:
        loop = asyncio.get_running_loop()
        for i in range(10):
            path = rf"\\.\pipe\discord-ipc-{i}"
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            try:
                transport, _ = await loop.create_pipe_connection(lambda: protocol, path)
            except OSError:
                continue
            return reader, asyncio.StreamWriter(transport, protocol, reader, loop), path
        raise OSError("no Discord pipe found; is Discord running?")
    for path in unix_candidates():
        if os.path.exists(path):
            try:
                r, w = await asyncio.open_unix_connection(path)
                return r, w, path
            except OSError:
                continue
    raise OSError("no Discord socket found; is Discord running?")


# ----- WebSocket (server side: unmasked frames out, masked frames in) -----

def ws_frame(payload, opcode=2):
    n = len(payload)
    first = 0x80 | opcode
    if n < 126:
        return bytes([first, n]) + payload
    if n < 65536:
        return bytes([first, 126]) + struct.pack(">H", n) + payload
    return bytes([first, 127]) + struct.pack(">Q", n) + payload


async def ws_read(reader):
    b0, b1 = await reader.readexactly(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    key = await reader.readexactly(4) if b1 & 0x80 else None
    data = await reader.readexactly(n)
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return b0 & 0x0F, data


# ----- relaying -----

def set_keepalive(writer):
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 15), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError:
        pass


async def pump(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def relay_raw(peer, cr, cw):
    dr, dw, name = await open_discord()
    log.info("%s: relaying to %s", peer, name)
    await asyncio.gather(pump(cr, dw), pump(dr, cw))


async def relay_ws(peer, cr, cw):
    dr, dw, name = await open_discord()
    log.info("%s: relaying to %s (WebSocket)", peer, name)

    async def ws_to_discord():
        try:
            while True:
                op, data = await ws_read(cr)
                if op in (0, 1, 2):
                    dw.write(data)
                    await dw.drain()
                elif op == 9:
                    cw.write(ws_frame(data, 10))
                    await cw.drain()
                elif op == 8:
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        dw.close()

    async def discord_to_ws():
        try:
            while data := await dr.read(65536):
                cw.write(ws_frame(data))
                await cw.drain()
        except (ConnectionError, OSError):
            pass
        try:
            cw.write(ws_frame(b"", 8))
        except (OSError, RuntimeError):
            pass
        cw.close()

    await asyncio.gather(ws_to_discord(), discord_to_ws())


# ----- listeners -----

class Receiver:
    def __init__(self, token, allow, path_by_port):
        self.token = token.encode() if token else b""
        self.allow = set(allow)
        self.path_by_port = path_by_port

    def token_ok(self, presented):
        return not self.token or hmac.compare_digest(presented, self.token)

    async def handle(self, cr, cw, kind, path):
        peer = cw.get_extra_info("peername")[0]
        try:
            if self.allow and peer not in self.allow:
                log.info("%s: rejected (not in --allow)", peer)
                return
            set_keepalive(cw)
            if kind == "raw":
                if self.token:
                    line = await asyncio.wait_for(cr.readline(), 5)
                    if not line.startswith(b"AUTH ") or not self.token_ok(line[5:].rstrip(b"\r\n")):
                        log.info("%s: rejected (bad or missing token)", peer)
                        return
                await relay_raw(peer, cr, cw)
            else:
                await self.handle_http(peer, cr, cw, path)
        except OSError as e:
            log.warning("%s: %s", peer, e)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            cw.close()

    async def handle_http(self, peer, cr, cw, path):
        head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 10)
        request, *lines = head.decode("latin-1").split("\r\n")
        method, target, _ = (request.split(" ") + ["", "", ""])[:3]
        headers = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines if ":" in l}

        def respond(status, body=b""):
            cw.write(f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)

        if urllib.parse.urlsplit(target).path != path:
            respond("404 Not Found")
            return
        if headers.get("upgrade", "").lower() != "websocket":
            respond("200 OK", b"discord receiver ok\n")
            return
        auth = headers.get("authorization", "")
        presented = auth[7:].encode() if auth.lower().startswith("bearer ") else b""
        if not self.token_ok(presented):
            log.info("%s: rejected (bad or missing token)", peer)
            respond("401 Unauthorized")
            return
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        cw.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await cw.drain()
        await relay_ws(peer, cr, cw)


def self_signed(directory):
    os.makedirs(directory, exist_ok=True)
    cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        log.info("Creating a self-signed certificate in %s", directory)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", key, "-out", cert, "-days", "3650",
                        "-subj", "/CN=discord-receiver"], check=True, capture_output=True)
        try:
            os.chmod(key, 0o600)
        except OSError:
            pass
    return cert, key


def fingerprint(cert_path):
    der = ssl.PEM_cert_to_DER_cert(open(cert_path).read())
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)).upper()


async def main():
    p = argparse.ArgumentParser(description="Discord Presence receiver (advanced)")
    p.add_argument("--listen", action="append", default=[],
                   help="tcp://, tls://, http:// or https:// address (repeatable); default tcp://0.0.0.0:8920")
    p.add_argument("--token", default=os.environ.get("DISCORD_RECEIVER_TOKEN", ""))
    p.add_argument("--allow", action="append", default=[], help="client address allowed to connect (repeatable)")
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--self-signed", metavar="DIR", help="create/reuse a self-signed certificate in DIR")
    a = p.parse_args()

    listens = a.listen or ["tcp://0.0.0.0:8920"]
    ctx = None
    if any(u.split("://")[0] in ("tls", "https") for u in listens):
        cert, key = (a.cert, a.key) if a.cert else self_signed(a.self_signed) if a.self_signed else (None, None)
        if not cert:
            sys.exit("tls:// and https:// need --cert/--key or --self-signed DIR.")
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        log.info("Certificate fingerprint (SHA-256): %s", fingerprint(cert))

    rx = Receiver(a.token, a.allow, {})
    servers = []
    for url in listens:
        u = urllib.parse.urlsplit(url if "://" in url else "tcp://" + url)
        scheme = u.scheme.lower()
        if scheme not in ("tcp", "tls", "http", "https"):
            sys.exit(f"Unsupported listener {url}")
        kind = "raw" if scheme in ("tcp", "tls") else "http"
        path = u.path or "/"
        use_ssl = ctx if scheme in ("tls", "https") else None
        port = u.port or {"http": 80, "https": 443}.get(scheme, 8920)
        server = await asyncio.start_server(lambda r, w, k=kind, pth=path: rx.handle(r, w, k, pth),
                                            u.hostname or "0.0.0.0", port, ssl=use_ssl)
        servers.append(server)
        log.info("Listening on %s://%s:%d%s", scheme, u.hostname or "0.0.0.0", port, path if kind == "http" else "")
        if a.token and scheme in ("tcp", "http") and u.hostname not in ("127.0.0.1", "localhost", "::1"):
            log.warning("The token crosses the network unencrypted on %s; prefer TLS or a private network.", url)
    log.info("Token: %s. Allowed addresses: %s", "required" if a.token else "none",
             ", ".join(a.allow) or "any")
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    if IS_WINDOWS:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
