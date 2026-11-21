"""Tests for py_modules/proton_bridge.py. Run: python3 -u tests/test_proton_bridge.py"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
import proton_bridge as pb  # noqa: E402

BRIDGE = "/home/deck/homebrew/plugins/DiscordStatusRelay/bin/bridge.exe"
SOCKET = "/run/user/1000/discord-ipc-0"


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.path = pb.conf_path(self.home)

    def test_write_is_idempotent_then_remove(self):
        self.assertTrue(pb.apply(True, self.home, BRIDGE, SOCKET))
        self.assertFalse(pb.apply(True, self.home, BRIDGE, SOCKET))
        self.assertTrue(pb.apply(True, self.home, BRIDGE, "/tmp/other"))   # socket moved
        self.assertTrue(pb.apply(False, self.home, BRIDGE, SOCKET))
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(pb.apply(False, self.home, BRIDGE, SOCKET))

    def test_removes_file_from_earlier_name(self):
        old = os.path.join(self.home, ".config", "environment.d", "50-discord-presence-proton.conf")
        os.makedirs(os.path.dirname(old))
        with open(old, "w") as f:
            f.write("# Written by the Discord Presence Decky plugin\nX=1\n")
        self.assertTrue(pb.apply(True, self.home, BRIDGE, SOCKET))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(self.path))

    def test_leaves_foreign_file_alone(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("FOO=bar\n")
        self.assertFalse(pb.apply(False, self.home, BRIDGE, SOCKET))
        self.assertTrue(os.path.exists(self.path))

    def test_shell_sourcing_matches_gamescope_session(self):
        """Game Mode sources the file with `set -a`; check what Steam would inherit."""
        pb.apply(True, self.home, BRIDGE, SOCKET)
        for existing, expected in [("", f"{BRIDGE}:{SOCKET}"),
                                   ("/srv/x", f"{BRIDGE}:{SOCKET}:/srv/x")]:
            env = {"PATH": os.environ["PATH"]}
            if existing:
                env["PRESSURE_VESSEL_FILESYSTEMS_RW"] = existing
            out = subprocess.run(
                ["bash", "-c", f'set -a; . "{self.path}"; set +a; '
                 'printf "%s\\n%s\\n%s\\n" "$PROTON_REMOTE_DEBUG_CMD" "$PRESSURE_VESSEL_FILESYSTEMS_RW" "$BRIDGE_PATH"'],
                env=env, capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertEqual(out, [f"{BRIDGE} --steam", expected, BRIDGE])


class StatusTests(unittest.TestCase):
    def fake_proc(self, env):
        proc = tempfile.mkdtemp()
        if env is not None:
            d = os.path.join(proc, "4242")
            os.makedirs(d)
            with open(os.path.join(d, "comm"), "w") as f:
                f.write("steam\n")
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        return proc

    def test_states(self):
        on = self.fake_proc({"PROTON_REMOTE_DEBUG_CMD": f"{BRIDGE} --steam"})
        off = self.fake_proc({"HOME": "/home/deck"})
        none = self.fake_proc(None)
        self.assertEqual(pb.status(True, BRIDGE, True, on), "active")
        self.assertEqual(pb.status(True, BRIDGE, True, off), "restart")
        self.assertEqual(pb.status(False, BRIDGE, True, on), "restart")
        self.assertEqual(pb.status(False, BRIDGE, True, off), "off")
        self.assertEqual(pb.status(True, BRIDGE, True, none), "unknown")
        self.assertEqual(pb.status(True, BRIDGE, False, on), "unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
