"""Rich presence for Windows games under Proton.

A Proton game talks to Discord over the named pipe \\\\.\\pipe\\discord-ipc-0 inside its
Wine prefix, which never reaches the hub's Unix socket. rpc-bridge (bin/bridge.exe) runs
inside the prefix and copies that pipe to the socket.

Proton starts PROTON_REMOTE_DEBUG_CMD next to every game, so setting it for the whole
Steam session covers every Proton game with no per-game launch options. Game Mode
(gamescope-session-plus) sources ~/.config/environment.d/*.conf when it starts, and
systemd reads the same files for the desktop session, so one file does it. Steam only
reads its environment at start: a change applies after Game Mode restarts.
"""
import os
import shlex

CONF_NAME = "50-discord-presence-proton.conf"

# Written at the top of the file; also how we recognise a file we own.
MARKER = "# Written by the Discord Presence Decky plugin"


def conf_path(home):
    return os.path.join(home, ".config", "environment.d", CONF_NAME)


def render(bridge_path, socket_path):
    """The environment.d file. Valid both as shell (Game Mode sources it) and for systemd."""
    share = f"{bridge_path}:{socket_path}"
    return "\n".join([
        MARKER,
        "# Runs rpc-bridge inside every Proton game so its own Discord status reaches the plugin.",
        "# Turn it off from the plugin, or delete this file and restart Game Mode.",
        f"BRIDGE_PATH={bridge_path}",
        f"PROTON_REMOTE_DEBUG_CMD={shlex.quote(f'{bridge_path} --steam')}",
        # Proton's container only sees paths it is told about: the bridge and the hub socket.
        "PRESSURE_VESSEL_FILESYSTEMS_RW="
        f"{share}${{PRESSURE_VESSEL_FILESYSTEMS_RW:+:$PRESSURE_VESSEL_FILESYSTEMS_RW}}",
        "",
    ])


def apply(enabled, home, bridge_path, socket_path):
    """Writes or removes the file. Returns True when it changed.

    Only ever removes a file carrying our marker, so a hand-written file of the same
    name is left alone."""
    path = conf_path(home)
    try:
        with open(path) as f:
            current = f.read()
    except FileNotFoundError:
        current = None

    if not enabled:
        if current is None or not current.startswith(MARKER):
            return False
        os.remove(path)
        return True

    wanted = render(bridge_path, socket_path)
    if current == wanted:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(wanted)
    os.replace(tmp, path)
    return True


def steam_environment(proc="/proc"):
    """Environment of the running Steam client, or None when Steam is not running."""
    uid = os.getuid()
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                if f.read().strip() != "steam":
                    continue
            if os.stat(os.path.join(proc, pid)).st_uid != uid:
                continue
            with open(os.path.join(proc, pid, "environ"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        env = {}
        for item in raw.split(b"\0"):
            key, sep, value = item.partition(b"=")
            if sep:
                env[key.decode(errors="replace")] = value.decode(errors="replace")
        return env
    return None


def status(enabled, bridge_path, available, proc="/proc"):
    """One of: "unavailable", "off", "active", "restart" (setting and Steam disagree), "unknown"."""
    if not available:
        return "unavailable"
    env = steam_environment(proc)
    if env is None:
        return "unknown"
    running = bridge_path in env.get("PROTON_REMOTE_DEBUG_CMD", "")
    if enabled:
        return "active" if running else "restart"
    return "restart" if running else "off"
