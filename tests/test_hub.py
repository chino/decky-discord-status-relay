"""End-to-end tests for the hub and the Python receiver, with a fake Discord.

No Decky or real Discord needed. Run: python3 -u tests/test_hub.py
"""
import asyncio
import functools
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
from hub import Hub, Config, frame, read_frame, parse, parse_target, OP_HANDSHAKE, OP_FRAME  # noqa: E402

RECEIVER = os.path.join(ROOT, "receivers", "python", "discord-receiver.py")
print = functools.partial(print, flush=True)
logging.basicConfig(level=logging.WARNING)


class FakeDiscord:
    """Records the current activity per connection, the way Discord does."""

    def __init__(self, path):
        self.path, self.shown, self.server, self.writers = path, {}, None, set()

    async def start(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.server = await asyncio.start_unix_server(self.conn, path=self.path)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()
        for w in list(self.writers):
            w.close()
        os.unlink(self.path)
        self.shown.clear()

    async def conn(self, r, w):
        self.writers.add(w)
        key, cid, ready = object(), None, False
        try:
            while True:
                op, body = await read_frame(r)
                m = parse(body)
                if op == OP_HANDSHAKE:
                    cid, ready = m["client_id"], True
                    w.write(frame(OP_FRAME, {"cmd": "DISPATCH", "evt": "READY", "nonce": None, "data": {"v": 1}}))
                elif m.get("cmd") == "SET_ACTIVITY":
                    if not ready:
                        continue  # Discord ignores commands before READY
                    act = (m.get("args") or {}).get("activity")
                    if act:
                        self.shown[key] = (cid, act.get("details"))
                    else:
                        self.shown.pop(key, None)
                    w.write(frame(OP_FRAME, {"cmd": "SET_ACTIVITY", "evt": None, "nonce": m.get("nonce"), "data": act}))
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.shown.pop(key, None)
            self.writers.discard(w)

    def details(self):
        return sorted(d for _, d in self.shown.values())


class Client:
    def __init__(self, path, pid):
        self.path, self.pid = path, pid

    async def connect(self, cid="111"):
        self.r, self.w = await asyncio.open_unix_connection(self.path)
        self.w.write(frame(OP_HANDSHAKE, {"v": 1, "client_id": cid}))
        await self.w.drain()
        assert parse((await read_frame(self.r))[1]).get("evt") == "READY"

    async def set(self, details):
        act = {"details": details} if details else None
        self.w.write(frame(OP_FRAME, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                                      "args": {"pid": self.pid, "activity": act}}))
        await self.w.drain()
        await read_frame(self.r)

    def close(self):
        self.w.close()


async def wait_for(cond, t=8):
    for _ in range(int(t * 20)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def start_receiver(env_dir, *args):
    env = dict(os.environ, XDG_RUNTIME_DIR=env_dir)
    return subprocess.Popen([sys.executable, "-u", RECEIVER, *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


async def main():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name)

    # Target parsing.
    check("bare host gets the default port", parse_target("pc.lan") == ("tcp", "pc.lan", 8920, "/"))
    check("https target", parse_target("https://pc.ts.net/discord") == ("https", "pc.ts.net", 443, "/discord"))
    check("host:port", parse_target("10.0.0.5:9000")[2] == 9000)

    hub_dir, remote_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = hub_dir           # the handheld
    remote = FakeDiscord(os.path.join(remote_dir, "discord-ipc-0"))   # Discord on the PC
    await remote.start()
    cert_dir = os.path.join(remote_dir, "certs")
    procs = [
        start_receiver(remote_dir, "--listen", "tcp://127.0.0.1:18940", "--token", "s3cret"),
        start_receiver(remote_dir, "--listen", "http://127.0.0.1:18941/discord", "--token", "s3cret"),
        start_receiver(remote_dir, "--listen", "https://127.0.0.1:18942/discord", "--token", "s3cret",
                       "--self-signed", cert_dir),
    ]
    await asyncio.sleep(1.5)
    fp_line = next((l for l in procs[2].stdout if "fingerprint" in l), "")
    pin = fp_line.rsplit(" ", 1)[-1].strip()

    async def run_case(name, target, token="s3cret", fingerprint="", expect=True, sock="h.sock"):
        hub = Hub(Config(mode="remote", target=target, token=token, fingerprint=fingerprint,
                         socket_path=os.path.join(hub_dir, sock)), generic_pid=999)
        await hub.start()
        c = Client(hub.config.socket_path, 999)
        await c.connect("1001")
        text = f"Playing via {name}"
        await c.set(text)
        shown = await wait_for(lambda: text in remote.details(), 6 if expect else 2)
        check(f"{name}: {'delivered' if expect else 'refused'}", shown == expect)
        c.close()
        await hub.stop()
        await wait_for(lambda: text not in remote.details(), 3)

    await run_case("tcp + token", "127.0.0.1:18940")
    await run_case("tcp, wrong token", "127.0.0.1:18940", token="nope", expect=False)
    await run_case("http WebSocket + token", "http://127.0.0.1:18941/discord")
    await run_case("http, wrong token", "http://127.0.0.1:18941/discord", token="nope", expect=False)
    await run_case("https self-signed + pinned fingerprint", "https://127.0.0.1:18942/discord", fingerprint=pin)
    await run_case("https, wrong fingerprint", "https://127.0.0.1:18942/discord", fingerprint="00" * 32, expect=False)
    await run_case("https, self-signed without pin", "https://127.0.0.1:18942/discord", expect=False)

    # Generic status steps aside for a game's own, then returns; replay after Discord restarts.
    hub = Hub(Config(mode="auto", target="127.0.0.1:18940", token="s3cret",
                     socket_path=os.path.join(hub_dir, "discord-ipc-0")), generic_pid=999)
    await hub.start()
    plugin = Client(hub.config.socket_path, 999)
    await plugin.connect("1001")
    await plugin.set("Playing Celeste")
    check("generic status delivered", await wait_for(lambda: remote.details() == ["Playing Celeste"]))
    game = Client(hub.config.socket_path, 4242)
    await game.connect("2002")
    await game.set("Chapter 3")
    check("game's own status shown, generic withheld", await wait_for(lambda: remote.details() == ["Chapter 3"]))
    game.close()
    check("generic restored when the game quits", await wait_for(lambda: remote.details() == ["Playing Celeste"]))
    await remote.stop()
    await remote.start()
    check("status replayed after Discord restarts", await wait_for(lambda: remote.details() == ["Playing Celeste"], 15))

    # Live switch: a Discord starting on the handheld takes over, and quitting it falls back.
    local = FakeDiscord(os.path.join(hub_dir, "app", "com.discordapp.Discord", "discord-ipc-0"))
    await local.start()
    check("switches to a Discord that starts on the handheld",
          await wait_for(lambda: local.details() == ["Playing Celeste"] and remote.details() == [], 10))
    await local.stop()
    check("falls back to the remote when it quits", await wait_for(lambda: remote.details() == ["Playing Celeste"], 15))
    print("hub status:", hub.describe())

    for p in procs:
        p.terminate()
    shutil.rmtree(hub_dir, ignore_errors=True)
    shutil.rmtree(remote_dir, ignore_errors=True)
    print("ALL PASS" if ok else "SOME FAILED")
    os._exit(0 if ok else 1)


asyncio.run(main())
