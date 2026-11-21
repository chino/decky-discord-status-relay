"""Discord IPC hub: owns the Discord socket and routes every client to a Discord.

Games with rich presence, and this plugin's own "Playing X" status, connect to the
socket this hub serves. For each client the hub opens a connection to a real Discord:

- the Discord app on this device, when one is running (mode "auto" or "local"), or
- a receiver on another machine (mode "auto" or "remote"), given as a target:
    host                    plain TCP on the default port (8920)
    host:port, tcp://...    plain TCP
    tls://host:port         TLS
    http://host:port/path   WebSocket (ws:// also accepted)
    https://host/path       WebSocket over TLS (wss:// also accepted)
  An optional token is sent as "AUTH <token>" on TCP/TLS, or as an
  "Authorization: Bearer" header over HTTP(S). For self-signed certificates, set a
  SHA-256 fingerprint to pin instead of verifying against system CAs.

If the Discord end drops, the client stays connected: the hub answers it locally,
reconnects with backoff, replays the handshake, waits for READY, and replays the last
status. Remote links use TCP keepalive so a vanished machine is noticed in about 30 s.
In auto mode, clients move to a Discord that starts on this device and back to the
remote when it quits. While any other client shows a status, the plugin's own generic
status is withheld so Discord doesn't show the game twice.

Pure asyncio, standard library only, no Decky imports, so it can be tested on its own.
Discord IPC framing: 8-byte little-endian header (opcode, length) + JSON body.
"""
import asyncio
import base64
import glob
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import urllib.parse
import uuid

OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 3, 4
DEFAULT_PORT = 8920
MAX_BACKOFF = 10          # seconds between reconnect attempts, at most
LOCAL_CHECK_INTERVAL = 3  # seconds between checks for a Discord starting on this device
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(op, body):
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    return struct.pack("<II", op, len(body)) + body


async def read_frame(reader):
    op, n = struct.unpack("<II", await reader.readexactly(8))
    return op, await reader.readexactly(n)


def parse(body):
    if isinstance(body, dict):
        return body
    try:
        value = json.loads(body)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


# ----- targets -----

def parse_target(target):
    """Returns (scheme, host, port, path) or raises ValueError."""
    target = (target or "").strip()
    if not target:
        raise ValueError("no target")
    if "://" not in target:
        target = "tcp://" + target
    u = urllib.parse.urlsplit(target)
    scheme = u.scheme.lower()
    if scheme not in ("tcp", "tls", "http", "https", "ws", "wss"):
        raise ValueError(f"unsupported scheme {scheme}")
    if not u.hostname:
        raise ValueError("no host")
    port = u.port or {"http": 80, "ws": 80, "https": 443, "wss": 443}.get(scheme, DEFAULT_PORT)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return scheme, u.hostname, port, path


def normalize_fingerprint(fp):
    return (fp or "").replace(":", "").replace(" ", "").strip().lower()


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


# ----- minimal WebSocket (RFC 6455), enough to carry a byte stream -----

def ws_frame(payload, opcode=2, mask=True):
    n = len(payload)
    first = 0x80 | opcode
    bit = 0x80 if mask else 0
    if n < 126:
        header = bytes([first, bit | n])
    elif n < 65536:
        header = bytes([first, bit | 126]) + struct.pack(">H", n)
    else:
        header = bytes([first, bit | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        header += key
    return header + payload


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


class WSWriter:
    """Looks like a StreamWriter; each write becomes one binary WebSocket message."""

    def __init__(self, writer, mask):
        self.w, self.mask, self.task = writer, mask, None

    def write(self, data):
        self.w.write(ws_frame(data, 2, self.mask))

    async def drain(self):
        await self.w.drain()

    def get_extra_info(self, name, default=None):
        return self.w.get_extra_info(name, default)

    def close(self):
        try:
            self.w.write(ws_frame(b"", 8, self.mask))
        except (OSError, RuntimeError):
            pass
        self.w.close()
        if self.task:
            self.task.cancel()


def ws_stream(reader, writer, mask):
    """Turn a WebSocket connection into a (StreamReader, WSWriter) byte stream."""
    out = asyncio.StreamReader()
    wsw = WSWriter(writer, mask)

    async def feed():
        try:
            while True:
                op, data = await ws_read(reader)
                if op in (0, 1, 2):
                    out.feed_data(data)
                elif op == 9:  # ping
                    writer.write(ws_frame(data, 10, mask))
                    await writer.drain()
                elif op == 8:  # close
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        out.feed_eof()
        writer.close()

    wsw.task = asyncio.ensure_future(feed())
    return out, wsw


async def ws_connect(reader, writer, host, port, path, token):
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket",
             "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    status, *headers = head.decode("latin-1").split("\r\n")
    if " 101 " not in status + " ":
        raise ConnectionError(f"WebSocket upgrade refused: {status}")
    expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    fields = {h.split(":", 1)[0].strip().lower(): h.split(":", 1)[1].strip() for h in headers if ":" in h}
    if fields.get("sec-websocket-accept") != expected:
        raise ConnectionError("bad WebSocket accept key")
    return ws_stream(reader, writer, mask=True)


class Config:
    def __init__(self, mode="auto", target="", token="", fingerprint="", socket_path=""):
        self.mode = mode            # "auto", "local" or "remote"
        self.target = target.strip()
        self.token = token
        self.fingerprint = fingerprint
        self.socket_path = socket_path


class Hub:
    def __init__(self, config, logger=None, generic_pid=None):
        self.config = config
        self.log = logger or logging.getLogger("discord-hub")
        self.generic_pid = generic_pid if generic_pid is not None else os.getpid()
        self.sessions = set()
        self.server = None
        self.watcher = None
        self.status = "starting"

    # ----- server -----

    async def start(self):
        path = self.config.socket_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.lexists(path):
            os.unlink(path)
        self.server = await asyncio.start_unix_server(self._client, path=path)
        self.watcher = asyncio.ensure_future(self._watch_local())
        self.status = "listening"
        self.log.info("Hub listening on %s (mode %s)", path, self.config.mode)

    async def stop(self):
        if self.watcher:
            self.watcher.cancel()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for s in list(self.sessions):
            s.close()
        try:
            os.unlink(self.config.socket_path)
        except OSError:
            pass

    async def _client(self, reader, writer):
        session = Session(self, reader, writer)
        self.sessions.add(session)
        try:
            await session.run()
        finally:
            self.sessions.discard(session)
            self.reconcile()

    # ----- destinations -----

    def local_discord_socket(self):
        """A Discord socket file on this device that isn't ours, or None."""
        own = os.path.realpath(self.config.socket_path)
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        roots = sorted(glob.glob(os.path.join(runtime, "app", "*"))) + [runtime, "/tmp"]
        for root in roots:
            for i in range(10):
                path = os.path.join(root, f"discord-ipc-{i}")
                if os.path.exists(path) and os.path.realpath(path) != own:
                    return path
        return None

    async def open_local(self):
        path = self.local_discord_socket()
        if not path:
            raise OSError("no local Discord running")
        try:
            r, w = await asyncio.wait_for(asyncio.open_unix_connection(path), 3)
        except asyncio.TimeoutError:
            raise OSError(f"local Discord at {path} not answering")
        return r, w, f"local {path}"

    async def open_remote(self):
        scheme, host, port, path = parse_target(self.config.target)
        tls = scheme in ("tls", "https", "wss")
        ctx = None
        pin = normalize_fingerprint(self.config.fingerprint)
        if tls:
            ctx = ssl.create_default_context()
            if pin:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if tls else None), 8)
        set_keepalive(w)
        if tls and pin:
            der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if hashlib.sha256(der).hexdigest() != pin:
                w.close()
                raise OSError("certificate fingerprint does not match the pinned one")
        if scheme in ("http", "https", "ws", "wss"):
            r, w = await ws_connect(r, w, host, port, path, self.config.token)
        elif self.config.token:
            w.write(f"AUTH {self.config.token}\n".encode())
            await w.drain()
        return r, w, f"remote {self.config.target}"

    async def open_discord(self):
        """Connect to the configured Discord. Returns (reader, writer, label) or raises OSError."""
        mode = self.config.mode
        if mode == "local":
            return await self.open_local()
        if mode == "auto":
            try:
                return await self.open_local()
            except OSError:
                pass
        if not self.config.target:
            raise OSError("no remote target configured")
        try:
            return await self.open_remote()
        except (ValueError, ssl.SSLError) as e:
            raise OSError(str(e))

    async def local_discord_alive(self):
        try:
            r, w, _ = await self.open_local()
        except OSError:
            return False
        w.close()
        return True

    async def _watch_local(self):
        """In auto mode, move remote clients to a Discord that starts on this device."""
        while True:
            await asyncio.sleep(LOCAL_CHECK_INTERVAL)
            try:
                if self.config.mode != "auto":
                    continue
                remote = [s for s in self.sessions if s.up and s.label.startswith("remote")]
                if remote and await self.local_discord_alive():
                    for s in remote:
                        self.log.info("[%d] Discord started on this device, switching to it", s.id)
                        s.drop_up()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the watcher has to outlive anything one pass throws
                self.log.warning("Local Discord check failed: %r", e)

    # ----- generic status suppression -----

    def other_status_active(self):
        return any(s.has_activity and not s.is_generic for s in self.sessions)

    def reconcile(self):
        hide = self.other_status_active()
        for s in list(self.sessions):
            if s.is_generic:
                asyncio.ensure_future(s.set_suppressed(hide))

    def describe(self):
        live = [s for s in self.sessions if s.up]
        if not self.sessions:
            return f"{self.status}, no clients"
        return f"{len(self.sessions)} client(s), {len(live)} connected" + (
            f" via {live[0].label}" if live else ", waiting for Discord")


class Session:
    count = 0

    def __init__(self, hub, reader, writer):
        Session.count += 1
        self.id = Session.count
        self.hub, self.log = hub, hub.log
        self.cr, self.cw = reader, writer
        self.hello = None        # client's handshake body
        self.activity = None     # client's last SET_ACTIVITY body that had an activity
        self.ready_sent = False
        self.is_generic = False  # the plugin's own "Playing X" connection
        self.suppressed = False
        self.up = None
        self.label = ""
        self.quiet_nonces = set()  # replies to frames the hub sent itself
        self.closed = False

    @property
    def has_activity(self):
        return self.activity is not None

    def close(self):
        self.closed = True
        self.cw.close()

    async def to_client(self, op, body):
        if not self.closed:
            self.cw.write(frame(op, body))
            await self.cw.drain()

    async def to_discord(self, op, body, quiet=False):
        """Send to Discord if connected. Returns False when not connected."""
        if not self.up:
            return False
        if quiet:
            nonce = parse(body).get("nonce")
            if nonce:
                self.quiet_nonces.add(nonce)
        try:
            self.up.write(frame(op, body))
            await self.up.drain()
            return True
        except (ConnectionError, OSError, RuntimeError):
            self.drop_up()
            return False

    def drop_up(self):
        if self.up:
            self.up.close()
        self.up = None

    async def run(self):
        self.log.info("[%d] client connected", self.id)
        upstream = asyncio.ensure_future(self.upstream_loop())
        try:
            await self.from_client()
        finally:
            self.closed = True
            upstream.cancel()
            self.drop_up()
            self.cw.close()
            self.log.info("[%d] client gone", self.id)

    async def from_client(self):
        while True:
            try:
                op, body = await read_frame(self.cr)
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            msg = parse(body)
            if op == OP_CLOSE:
                return
            if op == OP_HANDSHAKE:
                self.hello = body
            elif op == OP_FRAME and msg.get("cmd") == "SET_ACTIVITY":
                args = msg.get("args") or {}
                self.is_generic = args.get("pid") == self.hub.generic_pid
                self.activity = body if args.get("activity") else None
                if self.is_generic:
                    self.suppressed = bool(self.activity) and self.hub.other_status_active()
                    if self.suppressed:
                        await self.answer_locally(op, msg, body)
                        continue
                else:
                    self.hub.reconcile()
            if not await self.to_discord(op, body):
                await self.answer_locally(op, msg, body)

    async def answer_locally(self, op, msg, body):
        """Stand in for Discord while it is unreachable (or the status is withheld)."""
        if op == OP_HANDSHAKE:
            self.ready_sent = True
            await self.to_client(OP_FRAME, {"cmd": "DISPATCH", "evt": "READY", "nonce": None, "data": {
                "v": 1, "config": {"cdn_host": "cdn.discordapp.com", "api_endpoint": "//discord.com/api",
                                   "environment": "production"},
                "user": {"id": "0", "username": "hub", "discriminator": "0", "avatar": None}}})
        elif op == OP_PING:
            await self.to_client(OP_PONG, body)
        elif op == OP_FRAME:
            args = msg.get("args") or {}
            await self.to_client(OP_FRAME, {"cmd": msg.get("cmd"), "evt": None, "nonce": msg.get("nonce"),
                                            "data": args.get("activity")})

    async def set_suppressed(self, hide):
        if hide == self.suppressed:
            return
        self.suppressed = hide
        self.log.info("[%d] %s the generic status", self.id, "withholding" if hide else "restoring")
        if hide:
            await self.to_discord(OP_FRAME, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                                             "args": {"pid": self.hub.generic_pid}}, quiet=True)
        elif self.activity:
            body = parse(self.activity)
            body["nonce"] = str(uuid.uuid4())
            await self.to_discord(OP_FRAME, json.dumps(body).encode(), quiet=True)

    async def upstream_loop(self):
        delay = 1
        last_error = None
        while not self.closed:
            if self.hello is None:
                await asyncio.sleep(0.1)
                continue
            try:
                reader, writer, label = await self.hub.open_discord()
            except (OSError, asyncio.TimeoutError, ConnectionError) as e:
                if str(e) != last_error:
                    self.log.info("[%d] no Discord yet: %s", self.id, e or type(e).__name__)
                    last_error = str(e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)
                continue
            delay, last_error = 1, None
            try:
                # Replay the handshake and wait for READY: Discord ignores commands sent before it.
                writer.write(frame(OP_HANDSHAKE, self.hello))
                await writer.drain()
                op, body = await asyncio.wait_for(read_frame(reader), 10)
                if parse(body).get("evt") != "READY":
                    self.log.warning("[%d] %s refused the handshake: %s", self.id, label, body[:200])
                    raise ConnectionError("handshake refused")
                if not self.ready_sent:
                    self.ready_sent = True
                    await self.to_client(op, body)
                if self.activity and not self.suppressed:
                    writer.write(frame(OP_FRAME, self.activity))
                    await writer.drain()
                    op, body = await asyncio.wait_for(read_frame(reader), 10)
                    if parse(body).get("evt") == "ERROR":
                        self.log.warning("[%d] Discord rejected the replayed status: %s", self.id, body[:200])
                self.up, self.label = writer, label
                self.log.info("[%d] connected to %s", self.id, label)
                while True:
                    op, body = await read_frame(reader)
                    nonce = parse(body).get("nonce")
                    if nonce in self.quiet_nonces:
                        self.quiet_nonces.discard(nonce)
                        continue
                    await self.to_client(op, body)
            except (asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.TimeoutError):
                pass
            writer.close()
            self.up = None
            if not self.closed:
                self.log.info("[%d] lost %s, reconnecting", self.id, label)
                await asyncio.sleep(0.5)
