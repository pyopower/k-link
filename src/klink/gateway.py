"""The gateway service: K-plug adapter <-> USRP over UDP.

The adapter plugs into the radio's K connector and behaves like a phone
(HFP Audio Gateway). This host takes the place of the speaker-mic
(HFP Hands-Free):

  * PTT   = AVRCP passthrough, acted on RELEASE: FF -> TX, REWIND -> RX.
  * TX    = the adapter opens an HFP "call" (SCO, mSBC) while keyed.
  * RX    = the adapter opens a "call" only while the radio outputs audio,
            so the presence of the PipeWire input node is our COS/squelch.

Network side: USRP (ASL chan_usrp, DVSwitch Analog_Bridge, ...).
"""
import asyncio
import logging
import re
import signal
import socket
import struct
import time

from .bt import Adapter

log = logging.getLogger("k-link")

FRAME_SAMPLES = 160                 # 20 ms at 8 kHz
FRAME_BYTES = FRAME_SAMPLES * 2
USRP_HDR = struct.Struct(">4sIIIIIII")
USRP_VOICE = 0


# ---------------------------------------------------------------- PipeWire

async def bluez_nodes(mac):
    """Names of the adapter's PipeWire nodes currently present."""
    p = await asyncio.create_subprocess_exec(
        "pw-cli", "ls", "Node", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    tag = mac.upper().replace(":", "_")
    return set(re.findall(r'node\.name = "(bluez_(?:input|output)\.%s[^"]*)"'
                          % tag, out.decode(errors="replace")))


def pw_cat(mode, target):
    """Raw 8 kHz mono s16 stream to/from a PipeWire node via pw-cat."""
    args = ["pw-record" if mode == "rec" else "pw-play", "--target", target,
            "--rate", "8000", "--channels", "1", "--format", "s16",
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


# ---------------------------------------------------------------- USRP

class Usrp(asyncio.DatagramProtocol):
    def __init__(self, gw):
        self.gw = gw
        self.seq = 0
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < USRP_HDR.size or data[:4] != b"USRP":
            return
        _, _, _, keyup, _, typ, _, _ = USRP_HDR.unpack_from(data)
        if typ != USRP_VOICE:
            return
        audio = data[USRP_HDR.size:USRP_HDR.size + FRAME_BYTES]
        self.gw.net_frame(bool(keyup),
                          audio if len(audio) == FRAME_BYTES else b"")

    def send(self, keyup, audio=b""):
        if not self.transport:
            return
        pkt = USRP_HDR.pack(b"USRP", self.seq, 0, int(keyup),
                            self.gw.talkgroup, USRP_VOICE, 0, 0) + audio
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        self.transport.sendto(pkt, self.gw.remote)


# ---------------------------------------------------------------- gateway

class Gateway:
    IDLE, TX, RX = "idle", "tx", "rx"

    def __init__(self, cfg):
        self.cfg = cfg
        self.bt = Adapter(cfg.get("bluetooth", "mac"))
        self.listen = (cfg.get("usrp", "listen_host", fallback="0.0.0.0"),
                       cfg.getint("usrp", "listen_port"))
        self.remote = (cfg.get("usrp", "remote_host"),
                       cfg.getint("usrp", "remote_port"))
        self.talkgroup = cfg.getint("usrp", "talkgroup", fallback=0)
        self.tx_timeout = cfg.getfloat("ptt", "tx_timeout", fallback=180)
        self.net_hang = cfg.getfloat("ptt", "net_hang", fallback=0.5)
        self.rx_ignore = cfg.getfloat("ptt", "rx_ignore_after_tx",
                                      fallback=1.5)
        self.state = self.IDLE
        self.usrp = None
        self.txq = asyncio.Queue(maxsize=500)
        self.last_net = 0.0
        self.rx_blocked_until = 0.0
        self.nodes = set()
        self.tx_task = None
        self.link_up = False

    # --- network -> radio
    def net_frame(self, keyup, audio):
        self.last_net = time.monotonic()
        if not keyup:
            self.last_net = 0.0          # explicit unkey: end now
            return
        if self.state == self.RX or not self.link_up:
            return                       # half duplex / no Bluetooth link
        if self.state == self.IDLE:
            self.state = self.TX
            self.tx_task = asyncio.get_running_loop().create_task(
                self.do_tx())
        if audio and not self.txq.full():
            self.txq.put_nowait(audio)

    async def do_tx(self):
        log.info("TX start (from network)")
        t0 = time.monotonic()
        player = None
        try:
            if not await self.bt.ptt_down():
                return
            out = None
            while time.monotonic() - t0 < 3:
                out = next((n for n in self.nodes if "output" in n), None)
                if out:
                    break
                await asyncio.sleep(0.05)
            if not out:
                log.warning("TX: no SCO output node from the adapter")
            else:
                player = await pw_cat("play", out)
            while True:
                if time.monotonic() - t0 > self.tx_timeout:
                    log.warning("TX timeout (%.0f s)", self.tx_timeout)
                    break
                if time.monotonic() - self.last_net > self.net_hang \
                        and self.txq.empty():
                    break
                try:
                    a = await asyncio.wait_for(self.txq.get(), 0.1)
                except asyncio.TimeoutError:
                    continue
                if player and player.returncode is None:
                    player.stdin.write(a)
                    await player.stdin.drain()
        except asyncio.CancelledError:
            log.info("TX interrupted")
        except Exception:
            log.exception("TX error")
        finally:
            if player:
                try:
                    player.stdin.close()
                    await asyncio.wait_for(player.wait(), 1)
                except Exception:
                    pass
                await stop_proc(player)
            await self.unkey()
            while not self.txq.empty():
                self.txq.get_nowait()
            self.rx_blocked_until = time.monotonic() + self.rx_ignore
            self.state = self.IDLE
            log.info("TX end (%.1f s)", time.monotonic() - t0)

    async def unkey(self):
        if not await self.bt.ptt_up():
            # can't release the radio: dropping the link always unkeys it
            log.error("PTT up failed, disconnecting adapter")
            await self.bt.disconnect()

    # --- radio -> network
    async def do_rx(self, node):
        self.state = self.RX
        log.info("RX start (COS)")
        t0 = time.monotonic()
        rec = await pw_cat("rec", node)
        try:
            while node in self.nodes:
                try:
                    a = await asyncio.wait_for(
                        rec.stdout.readexactly(FRAME_BYTES), 0.2)
                except asyncio.TimeoutError:
                    continue
                except asyncio.IncompleteReadError:
                    break
                self.usrp.send(True, a)
        finally:
            await stop_proc(rec)
            self.usrp.send(False)
            self.state = self.IDLE
            log.info("RX end (%.1f s)", time.monotonic() - t0)

    # --- supervision
    async def watch(self):
        """Keep the Bluetooth link up and track PipeWire nodes (COS)."""
        last_check = 0.0
        while True:
            now = time.monotonic()
            if now - last_check > 5 and self.state == self.IDLE:
                last_check = now
                self.link_up = await self.bt.connected()
                if not self.link_up:
                    log.info("adapter not connected, connecting...")
                    self.link_up = await self.bt.connect()
                    log.info("connect: %s", "ok" if self.link_up else "failed")
            self.nodes = await bluez_nodes(self.bt.mac)
            if self.state == self.IDLE and now >= self.rx_blocked_until:
                inp = next((n for n in self.nodes if "input" in n), None)
                if inp:
                    asyncio.get_running_loop().create_task(self.do_rx(inp))
            await asyncio.sleep(0.1)

    async def run(self):
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)

        def mk():
            self.usrp = Usrp(self)
            return self.usrp
        await loop.create_datagram_endpoint(mk, local_addr=self.listen,
                                            family=socket.AF_INET)
        log.info("USRP listen %s:%d -> %s:%d, adapter %s",
                 *self.listen, *self.remote, self.bt.mac)
        watcher = loop.create_task(self.watch())
        await stop.wait()
        log.info("stopping")
        watcher.cancel()
        if self.tx_task and not self.tx_task.done():
            self.tx_task.cancel()        # its finally: unkeys the radio
            await asyncio.gather(self.tx_task, return_exceptions=True)
