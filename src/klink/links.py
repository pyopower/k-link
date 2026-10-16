"""Network/application side of the gateway ("links").

A link feeds audio to transmit into the gateway (gw.tx_frame) and receives
what the radio hears (on_rx / on_rx_end). Several links can run at once;
the first one that starts a transmission owns it until it ends.
"""
import asyncio
import collections
import fcntl
import logging
import socket
import struct
import time

from .audio import FRAME_BYTES, frame_dbfs
from .serialport import VirtualSerial

log = logging.getLogger("k-link")

F_SETPIPE_SZ = 1031          # fcntl.F_SETPIPE_SZ (Python 3.10+)
USRP_HDR = struct.Struct(">4sIIIIIII")
USRP_VOICE = 0


# ---------------------------------------------------------------- USRP

class UsrpLink(asyncio.DatagramProtocol):
    """USRP over UDP: AllStarLink chan_usrp, DVSwitch Analog_Bridge, ..."""

    name = "usrp"

    def __init__(self, gw, cfg):
        self.gw = gw
        self.listen = (cfg.get("usrp", "listen_host", fallback="0.0.0.0"),
                       cfg.getint("usrp", "listen_port", fallback=34001))
        self.remote = (cfg.get("usrp", "remote_host", fallback="127.0.0.1"),
                       cfg.getint("usrp", "remote_port", fallback=32001))
        self.talkgroup = cfg.getint("usrp", "talkgroup", fallback=0)
        self.seq = 0
        self.transport = None
        self.last_in = self.last_out = 0.0

    async def start(self):
        await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: self, local_addr=self.listen, family=socket.AF_INET)
        log.info("USRP listen %s:%d -> %s:%d", *self.listen, *self.remote)

    async def stop(self):
        if self.transport:
            self.transport.close()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < USRP_HDR.size or data[:4] != b"USRP":
            return
        self.last_in = time.monotonic()
        _, _, _, keyup, _, typ, _, _ = USRP_HDR.unpack_from(data)
        if typ != USRP_VOICE:
            return
        audio = data[USRP_HDR.size:USRP_HDR.size + FRAME_BYTES]
        self.gw.tx_frame(self, bool(keyup),
                         audio if len(audio) == FRAME_BYTES else b"")

    def _send(self, keyup, audio=b""):
        if self.transport:
            self.transport.sendto(
                USRP_HDR.pack(b"USRP", self.seq, 0, int(keyup),
                              self.talkgroup, USRP_VOICE, 0, 0) + audio,
                self.remote)
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            self.last_out = time.monotonic()

    def info(self):
        return {"ok": self.transport is not None,
                "detail": "%d ⇄ %s:%d" % (self.listen[1], *self.remote),
                "last_in": self.last_in, "last_out": self.last_out}

    def on_rx(self, frame):
        self._send(True, frame)

    def on_rx_end(self):
        self._send(False)


# ---------------------------------------------------------------- sound card

class SoundcardLink:
    """Virtual sound card for any audio application.

    Two PipeWire nodes appear on the system:
      "k-link: to radio"   (speaker)    what apps play here is transmitted;
                                        PTT by signal (VOX) with pre-roll
      "k-link: from radio" (microphone) what the radio receives
    PTT is by signal (VOX) or, with control = serial, by the RTS/DTR line
    of the virtual serial port /dev/ttyKLINK0, whose CTS/DCD carry the COS.
    """

    name = "soundcard"

    def __init__(self, gw, cfg):
        self.gw = gw
        self.threshold = cfg.getfloat("soundcard", "vox_threshold_db",
                                      fallback=-45)
        self.hang = cfg.getfloat("soundcard", "vox_hang", fallback=1.0)
        # the gateway ends our TX on our explicit unkey; this is a fallback
        self.tx_hang = self.hang + 1.0
        self.preroll = collections.deque(
            maxlen=max(1, int(cfg.getfloat("soundcard", "vox_preroll",
                                           fallback=0.3) / 0.02)))
        self.sink = self.source = self.nodes = None
        self.last_in = self.last_out = 0.0
        self.serial = None
        self.ptt_since = None            # serial PTT asserted at (monotonic)
        if cfg.get("soundcard", "control", fallback="vox") == "serial":
            self.serial = VirtualSerial(
                cfg.get("soundcard", "serial_device", fallback="ttyKLINK0"),
                cfg.get("soundcard", "serial_ptt_pin", fallback="rts"),
                cfg.get("soundcard", "serial_cos_pins", fallback="cts,dcd"),
                on_ptt=self._serial_ptt)
            # unkeys come explicitly from the PTT line
            self.tx_hang = 1.0
        self.tasks = []

    async def start(self):
        # Real (null) nodes, not streams posing as devices: those upset the
        # graph's clocking. The nodes live as long as this pw-cli process.
        self.nodes = await asyncio.create_subprocess_exec(
            "pw-cli", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        for name, cls, desc in (
                ("klink_to_radio", "Audio/Sink", "k-link: to radio"),
                ("klink_from_radio", "Audio/Source/Virtual",
                 "k-link: from radio")):
            # very low priority: never take over from a real sound card
            self.nodes.stdin.write((
                "create-node adapter { factory.name=support.null-audio-sink "
                f"node.name={name} node.description=\"{desc}\" "
                f"media.class={cls} audio.channels=1 audio.position=[MONO] "
                "priority.session=1 priority.driver=0 "
                "node.want-driver=true }\n").encode())
        await self.nodes.stdin.drain()
        await asyncio.sleep(0.5)
        common = ["--rate", "8000", "--channels", "1", "--format", "s16",
                  "--latency", "40ms"]
        # apps -> radio: record what is played into "to radio" (its monitor);
        # stdbuf: pw-record otherwise writes to the pipe in 0.5 s chunks
        self.sink = await asyncio.create_subprocess_exec(
            "stdbuf", "-o0", "pw-record", "--target", "klink_to_radio",
            "-P", "{ node.name=klink_sc_rec stream.capture.sink=true "
            "node.dont-reconnect=true }", *common, "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        # radio -> apps: a stream linked by hand into "from radio"
        # (--target 0: the session manager must not link it anywhere else)
        self.source = await asyncio.create_subprocess_exec(
            "pw-play", "--target", "0",
            "-P", "{ node.name=klink_sc_play }", *common, "-",
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:   # small pipe (~128 ms) so audio can't pile up in the kernel
            fcntl.fcntl(self.source.stdin.transport.get_extra_info("pipe")
                        .fileno(), F_SETPIPE_SZ, 4096)
        except OSError:
            pass
        # the stream only finishes negotiating once it has data
        self.source.stdin.write(bytes(FRAME_BYTES * 5))
        for _ in range(15):
            p = await asyncio.create_subprocess_exec(
                "pw-link", "klink_sc_play:output_MONO",
                "klink_from_radio:input_MONO",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                if await asyncio.wait_for(p.wait(), 3) == 0:
                    break
            except asyncio.TimeoutError:
                p.kill()        # the link lingers once created
                await p.wait()
                break
            await asyncio.sleep(0.2)
        else:
            log.error("sound card: could not link 'from radio'")
        if self.serial:
            try:
                await self.serial.start()
            except OSError as e:
                self.gw.event(f"serial port: cannot use /dev/cuse ({e}); "
                              "run: k-link setup-serial", logging.ERROR)
                self.serial.fd = None
            loop_fn = self._serial_tx
        else:
            loop_fn = self._vox
        self.tasks = [asyncio.get_running_loop().create_task(loop_fn())]
        log.info("sound card: 'k-link: to radio' / 'k-link: from radio' "
                 "(%s)", f"PTT by {self.serial.path} "
                 f"{self.serial.ptt_mask_name()}" if self.serial else
                 f"VOX {self.threshold:.0f} dBFS, hang {self.hang:.1f} s")

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        if self.serial:
            await self.serial.stop()
        for p in (self.sink, self.source, self.nodes):
            if p and p.returncode is None:
                p.terminate()

    async def _vox(self):
        """App audio -> radio, keyed while there is signal."""
        active = False
        quiet = 0.0
        while True:
            try:
                f = await asyncio.wait_for(
                    self.sink.stdout.readexactly(FRAME_BYTES), 0.1)
            except asyncio.TimeoutError:
                # nothing is playing: the sink delivers no data at all
                self.gw.level["app"] = -99.0
                if active:
                    quiet += 0.1
                    if quiet >= self.hang:
                        active = False
                        self.gw.tx_frame(self, False, b"")
                continue
            except asyncio.IncompleteReadError:
                log.error("sound card: 'to radio' stream ended")
                return
            db = self.gw.level["app"] = frame_dbfs(f)
            loud = db > self.threshold
            if loud:
                self.last_in = time.monotonic()
            if not active:
                self.preroll.append(f)
                if loud:
                    active = True
                    quiet = 0.0
                    for p in self.preroll:          # keep the first syllable
                        self.gw.tx_frame(self, True, p)
                    self.preroll.clear()
                continue
            quiet = 0.0 if loud else quiet + 0.02
            if quiet >= self.hang:
                active = False
                self.gw.tx_frame(self, False, b"")
            else:
                self.gw.tx_frame(self, True, f)

    # --- PTT by serial line
    def _serial_ptt(self, on):
        if on:
            self.ptt_since = time.monotonic()
        else:
            self.ptt_since = None
            self.gw.tx_frame(self, False, b"")

    async def _serial_tx(self):
        """App audio -> radio while the application holds PTT."""
        # brief pulses (e.g. both lines raised when a port is opened)
        # must not key the radio
        debounce = 0.1
        while True:
            try:
                f = await asyncio.wait_for(
                    self.sink.stdout.readexactly(FRAME_BYTES), 0.1)
            except asyncio.TimeoutError:
                f = b""                  # keyed but nothing playing yet
            except asyncio.IncompleteReadError:
                log.error("sound card: 'to radio' stream ended")
                return
            self.gw.level["app"] = frame_dbfs(f) if f else -99.0
            t = self.ptt_since
            if t is not None and time.monotonic() - t >= debounce:
                self.last_in = time.monotonic()
                self.gw.tx_frame(self, True, f)

    def on_rx(self, frame):
        """Radio audio -> virtual microphone, never building up delay.

        pw-play plays silence while nothing arrives; if no application is
        recording, frames are dropped instead of queuing stale audio.
        """
        if self.serial:
            self.serial.set_cos(True)
        t = self.source.stdin.transport if self.source else None
        if t is None or t.is_closing():
            return
        if t.get_write_buffer_size() <= 2 * FRAME_BYTES:
            t.write(frame)
            self.last_out = time.monotonic()

    def info(self):
        procs = (self.sink, self.source, self.nodes)
        ok = all(p and p.returncode is None for p in procs)
        i = {"ok": ok, "detail": "k-link: to radio / k-link: from radio",
             "last_in": self.last_in, "last_out": self.last_out}
        if self.serial:
            s = self.serial
            i["serial"] = {"path": s.path, "ok": s.fd is not None,
                           "open": s.opened > 0, "ptt": s.ptt,
                           "cos": s.cos}
            i["ok"] = ok and s.fd is not None
        return i

    def on_rx_end(self):
        if self.serial:
            self.serial.set_cos(False)


LINKS = {"usrp": UsrpLink, "soundcard": SoundcardLink}
