"""Bluetooth side: the K-plug adapter as seen through BlueZ.

Only system tools are used (busctl, bluetoothctl, btmgmt), so k-link has no
Python dependencies beyond the standard library.
"""
import asyncio
import logging
import re
import shutil
import subprocess

log = logging.getLogger("k-link")

# The adapter pages the speaker-mic it is looking for; it connected to us when
# we presented ourselves like the Abbree mic: its name and a headset class.
MIC_NAME = "KST_vHMIC010"
HEADSET_CLASS = (4, 4)          # major Audio/Video, minor Wearable Headset


async def run(*args, timeout=10):
    p = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 1, ""
    return p.returncode, out.decode(errors="replace")


def dev_path(mac):
    return "/org/bluez/hci0/dev_" + mac.upper().replace(":", "_")


class Adapter:
    """Connection and PTT keys of a paired adapter."""

    def __init__(self, mac):
        self.mac = mac.upper()
        self.path = dev_path(self.mac)

    async def connected(self):
        rc, out = await run("busctl", "get-property", "org.bluez", self.path,
                            "org.bluez.Device1", "Connected")
        return rc == 0 and out.strip() == "b true"

    async def connect(self):
        rc, out = await run("bluetoothctl", "connect", self.mac, timeout=25)
        return "successful" in out

    async def disconnect(self):
        await run("bluetoothctl", "disconnect", self.mac)

    async def _key(self, method):
        rc, out = await run("busctl", "call", "org.bluez", self.path,
                            "org.bluez.MediaControl1", method)
        if rc:
            log.warning("AVRCP %s failed: %s", method, out.strip())
        return rc == 0

    async def _tap(self, key, release_with):
        # BlueZ holds FF/REWIND (repeating the press) until another key is
        # sent; the adapter acts on release, so release with a harmless key.
        ok = await self._key(key)
        await asyncio.sleep(0.3)
        await self._key(release_with)
        return ok

    async def ptt_down(self):
        """Key the radio (FF tap): the adapter opens the TX audio call."""
        return await self._tap("FastForward", "VolumeDown")

    async def ptt_up(self):
        """Unkey (REWIND tap)."""
        return await self._tap("Rewind", "VolumeUp")


# ---------------------------------------------------------------- pairing

def _controller():
    out = subprocess.run(["bluetoothctl", "show"], capture_output=True,
                         text=True).stdout
    alias = re.search(r"Alias: (.*)", out)
    cls = re.search(r"Class: (0x[0-9a-f]+)", out)
    return (alias.group(1).strip() if alias else None,
            int(cls.group(1), 16) if cls else None)


def _set_class(major, minor):
    """Device class needs CAP_NET_ADMIN: try btmgmt directly, then sudo -n."""
    for pre in ([], ["sudo", "-n"]):
        if subprocess.run(pre + ["btmgmt", "class", str(major), str(minor)],
                          capture_output=True).returncode == 0:
            return True
    return False


def pair(timeout=120, mic_name=MIC_NAME, say=print):
    """Pose as the speaker-mic until the adapter pairs with us.

    Returns the adapter MAC, or None. The controller alias and class are
    restored afterwards; a bonded adapter is then reconnected by MAC.
    """
    if not shutil.which("bluetoothctl"):
        raise RuntimeError("bluetoothctl not found (install bluez)")
    old_alias, old_class = _controller()
    if not _set_class(*HEADSET_CLASS):
        say("! could not set the headset device class (needs root: "
            "run with sudo or allow 'sudo -n btmgmt'); trying anyway")
    bc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, bufsize=1)

    def cmd(c):
        bc.stdin.write(c + "\n")
        bc.stdin.flush()

    for c in ("power on", "agent NoInputNoOutput", "default-agent",
              f"system-alias {mic_name}", "pairable on",
              f"discoverable-timeout {timeout}", "discoverable on"):
        cmd(c)
    say(f"Visible as '{mic_name}' for {timeout} s.")
    say("Switch off the speaker-mic, then put the K-plug adapter in pairing "
        "mode (fast blinking LED).")
    mac = None
    names = {}
    ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")
    import selectors
    import time
    sel = selectors.DefaultSelector()
    sel.register(bc.stdout, selectors.EVENT_READ)
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end and not mac:
            if not sel.select(timeout=1):
                continue
            line = ansi.sub("", bc.stdout.readline())
            if not line:
                break
            m = re.search(r"Device ([0-9A-F:]{17}) (?:Name: )?(.+)", line)
            if m and "RSSI" not in line:
                names.setdefault(m.group(1), m.group(2).strip())
            if "(yes/no)" in line:          # pairing / service authorization
                cmd("yes")
            m = re.search(r"Device ([0-9A-F:]{17}) (?:Paired|Bonded): yes",
                          line)
            if m:
                mac = m.group(1)
        if mac:
            cmd(f"trust {mac}")
            time.sleep(1)
            say(f"Paired with {mac} ({names.get(mac, '?')}), trusted.")
        else:
            say("No adapter paired in time.")
    finally:
        cmd("discoverable off")
        cmd("pairable off")
        if old_alias:
            cmd(f"system-alias {old_alias}")
        time.sleep(0.5)
        cmd("quit")
        try:
            bc.wait(3)
        except subprocess.TimeoutExpired:
            bc.kill()
        if old_class is not None:
            _set_class((old_class >> 8) & 0x1F, (old_class >> 2) & 0x3F)
    return mac
