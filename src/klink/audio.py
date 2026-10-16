"""PipeWire helpers and audio levels (8 kHz mono s16 frames of 20 ms)."""
import array
import asyncio
import math
import re

RATE = 8000
# peers the adapter's nodes may be linked to: our own streams and the
# internal null nodes (never the virtual sound card: that would be a loop)
ALLOWED_PEERS = ("klink_bt_", "klink_null_")
NULL_SINK = "klink_null_sink"       # internal: swallows the adapter's RX
NO_TARGET = "klink_none"            # nonexistent: the session manager waits
FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2


def frame_dbfs(frame):
    """RMS level of a s16 frame in dBFS (-99 for silence)."""
    a = array.array("h", frame)
    if not a:
        return -99.0
    ms = sum(x * x for x in a) / len(a)
    return 10 * math.log10(ms / 32768 ** 2) if ms else -99.0


async def bluez_nodes(mac, with_ids=False):
    """Names (or {name: id}) of the adapter's PipeWire nodes now present."""
    p = await asyncio.create_subprocess_exec(
        "pw-cli", "ls", "Node", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 3)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return {} if with_ids else set()
    tag = mac.upper().replace(":", "_")
    found = {}
    node_id = None
    for line in out.decode(errors="replace").splitlines():
        m = re.match(r"\s*id (\d+), type PipeWire:Interface:Node", line)
        if m:
            node_id = int(m.group(1))
            continue
        m = re.search(r'node\.name = "(bluez_(?:input|output)\.%s[^"]*)"'
                      % tag, line)
        if m:
            found[m.group(1)] = node_id
    return found if with_ids else set(found)


def pw_cat(mode, target):
    """Raw 8 kHz mono s16 stream to/from a PipeWire node via pw-cat."""
    # pw-record buffers 8 KB (0.5 s) before writing to a pipe: unbuffer it
    # if the target isn't ready yet the session manager would fall back to
    # the default device (possibly k-link's own virtual sound card): forbid it
    args = (["stdbuf", "-o0", "pw-record"] if mode == "rec" else
            ["pw-play"]) + [
            "-P", "{ node.name=%s node.dont-fallback=true "
            "node.dont-reconnect=true }" % ("klink_bt_" + mode),
            "--target", target,
            "--rate", str(RATE), "--channels", "1", "--format", "s16",
            "--latency", "40ms", "-"]
    if mode == "rec":
        return asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
    return asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)


async def stop_proc(p):
    if p and p.returncode is None:
        p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 2)
        except asyncio.TimeoutError:
            p.kill()


async def unlink_foreign(mac):
    """Cut links between the adapter's nodes and anything but our streams.

    In the Hands-Free role PipeWire presents the adapter like a phone call:
    autoconnected streams that would play the radio's RX to the default
    speaker and feed the default microphone into its TX. The default devices
    may be k-link's own virtual sound card, which would close a loop.
    """
    p = await asyncio.create_subprocess_exec(
        "pw-link", "-l", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 3)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 0
    tag = "bluez_"
    mac_tag = mac.upper().replace(":", "_")
    cut = 0
    port = None
    for line in out.decode(errors="replace").splitlines():
        if not line.startswith(" "):
            port = line.strip()
            continue
        m = re.match(r"\s*\|(->|<-) (.+)", line)
        if not m or not port:
            continue
        other = m.group(2).strip()
        ours, peer = (port, other)
        bt = [x for x in (ours, peer) if x.startswith(tag) and mac_tag in x]
        if not bt:
            continue
        rest = [x for x in (ours, peer) if x not in bt]
        if rest and rest[0].startswith(ALLOWED_PEERS):
            continue
        out_p, in_p = (port, other) if m.group(1) == "->" else (other, port)
        r = await asyncio.create_subprocess_exec(
            "pw-link", "-d", out_p, in_p,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await r.wait()
        cut += 1
    return cut // 2 if cut else 0     # each link is listed from both ends


class InternalNodes:
    """A silent node with a clock that the adapter's RX stream attaches to.

    In the Hands-Free role the adapter's nodes are streams: left alone they
    follow the default speaker/microphone (the radio would sound on the PC
    speakers, the PC mic would transmit, or k-link's virtual sound card
    would close a loop). Pointing them at these null nodes keeps them
    isolated and gives the group a clock (our own streams can't drive it).
    The nodes live as long as the pw-cli process that created them.
    """

    def __init__(self):
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "pw-cli", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        for name, cls in ((NULL_SINK, "Audio/Sink"),):
            self.proc.stdin.write((
                "create-node adapter { factory.name=support.null-audio-sink "
                f"node.name={name} node.description=\"k-link internal (do not use)\" "
                f"media.class={cls} audio.channels=1 audio.position=[MONO] "
                "priority.session=0 priority.driver=0 "
                "node.want-driver=true node.always-process=true "
                "monitor.channel-volumes=false }\n").encode())
        await self.proc.stdin.drain()

    async def stop(self):
        await stop_proc(self.proc)


async def isolate(ids):
    """Point the adapter's streams ({name: id}) at the internal nodes."""
    for name, nid in ids.items():
        if nid is None:
            continue
        # RX stream -> our null sink (gives the group a clock, like the
        # "Dummy Output" fallback); TX stream -> nothing but our player
        target = NULL_SINK if name.startswith("bluez_input") else NO_TARGET
        p = await asyncio.create_subprocess_exec(
            "pw-metadata", str(nid), "target.object", target,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await p.wait()
