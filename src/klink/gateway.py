"""The gateway: K-plug adapter <-> links (USRP, virtual sound card).

The adapter plugs into the radio's K connector and behaves like a phone
(HFP Audio Gateway). This host takes the place of the speaker-mic
(HFP Hands-Free):

  * PTT   = AVRCP passthrough, acted on RELEASE: FF -> TX, REWIND -> RX.
  * TX    = the adapter opens an HFP "call" (SCO, mSBC) while keyed.
  * RX    = the adapter opens a "call" only while the radio outputs audio,
            so the presence of the PipeWire input node is our COS/squelch.
"""
import asyncio
import collections
import logging
import signal
import time

from .audio import (FRAME_BYTES, InternalNodes, bluez_nodes, frame_dbfs,
                    isolate, pw_cat, stop_proc, unlink_foreign)
from .bt import Adapter
from .links import LINKS

log = logging.getLogger("k-link")


class Gateway:
    IDLE, TX, RX, BUSY = "idle", "tx", "rx", "busy"

    def __init__(self, cfg, cfg_path=None):
        self.cfg = cfg
        self.cfg_path = cfg_path
        mac = cfg.get("bluetooth", "mac", fallback="").strip()
        self.bt = Adapter(mac) if mac else None
        self.tx_timeout = cfg.getfloat("ptt", "tx_timeout", fallback=180)
        self.net_hang = cfg.getfloat("ptt", "net_hang", fallback=0.5)
        self.rx_ignore = cfg.getfloat("ptt", "rx_ignore_after_tx",
                                      fallback=2.0)
        self.links = []
        for name, cls in LINKS.items():
            if cfg.getboolean(name, "enabled",
                              fallback=(name == "usrp")):
                self.links.append(cls(self, cfg))
        self.state = self.IDLE
        self.link_up = False
        self.txq = asyncio.Queue(maxsize=500)
        self.tx_owner = None
        self.last_tx_frame = 0.0
        self.rx_blocked_until = 0.0
        self.nodes = set()
        self.tx_task = None
        # for the web UI
        self.level = {"tx": -99.0, "rx": -99.0, "app": -99.0}
        self.stats = {"tx": 0, "rx": 0, "last_tx": None, "last_rx": None}
        self.events = collections.deque(maxlen=200)
        self.listeners = set()

    # --- events (log + web)
    def event(self, msg, level=logging.INFO):
        log.log(level, msg)
        e = {"t": time.time(), "msg": msg,
             "level": logging.getLevelName(level).lower()}
        self.events.append(e)
        for q in list(self.listeners):
            if not q.full():
                q.put_nowait(("event", e))

    def status(self):
        return {"state": self.state if self.bt else "unpaired",
                "link_up": self.link_up,
                "mac": self.bt.mac if self.bt else None,
                "tx_owner": getattr(self.tx_owner, "name", self.tx_owner),
                "links": [l.name for l in self.links],
                "level": self.level, "stats": self.stats}

    # --- links -> radio
    def tx_frame(self, link, keyup, audio):
        """Audio to transmit from a link; keyup False = end of over."""
        if self.tx_owner not in (None, link):
            return                       # another link owns the TX
        if not keyup:
            if self.tx_owner is link:
                self.last_tx_frame = 0.0  # explicit unkey: end now
            return
        if self.state in (self.RX, self.BUSY) or not self.link_up:
            return                       # half duplex / maintenance / no BT
        self.last_tx_frame = time.monotonic()
        if self.state == self.IDLE:
            self.state = self.TX
            self.tx_owner = link
            self.tx_task = asyncio.get_running_loop().create_task(
                self.do_tx())
        if audio and not self.txq.full():
            self.txq.put_nowait(audio)

    async def do_tx(self):
        who = getattr(self.tx_owner, "name", self.tx_owner)
        self.event(f"TX start ({who})")
        t0 = time.monotonic()
        hang = getattr(self.tx_owner, "tx_hang", self.net_hang)
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
                self.event("TX: the adapter did not open the audio",
                           logging.WARNING)
            else:
                player = await pw_cat("play", out)
            while True:
                if time.monotonic() - t0 > self.tx_timeout:
                    self.event(f"TX timeout ({self.tx_timeout:.0f} s)",
                               logging.WARNING)
                    break
                if time.monotonic() - self.last_tx_frame > hang \
                        and self.txq.empty():
                    break
                try:
                    a = await asyncio.wait_for(self.txq.get(), 0.1)
                except asyncio.TimeoutError:
                    continue
                self.level["tx"] = frame_dbfs(a)
                if player and player.returncode is None:
                    player.stdin.write(a)
                    await player.stdin.drain()
        except asyncio.CancelledError:
            self.event("TX interrupted")
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
            self.level["tx"] = -99.0
            dur = time.monotonic() - t0
            self.stats["tx"] += 1
            self.stats["last_tx"] = {"t": time.time(), "dur": dur, "by": who}
            self.tx_owner = None
            self.state = self.IDLE
            self.event(f"TX end ({dur:.1f} s)")

    async def unkey(self):
        if not await self.bt.ptt_up():
            # can't release the radio: dropping the link always unkeys it
            self.event("PTT up failed, disconnecting adapter", logging.ERROR)
            await self.bt.disconnect()

    # --- radio -> links
    async def do_rx(self, node):
        self.state = self.RX
        self.event("RX start")
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
                self.level["rx"] = frame_dbfs(a)
                for link in self.links:
                    link.on_rx(a)
        finally:
            await stop_proc(rec)
            for link in self.links:
                link.on_rx_end()
            self.level["rx"] = -99.0
            dur = time.monotonic() - t0
            self.stats["rx"] += 1
            self.stats["last_rx"] = {"t": time.time(), "dur": dur}
            if self.state == self.RX:
                self.state = self.IDLE
            self.event(f"RX end ({dur:.1f} s)")

    # --- supervision
    async def watch(self):
        """Keep the Bluetooth link up and track PipeWire nodes (COS)."""
        last_check = 0.0
        while True:
            try:
                last_check = await self._watch_once(last_check)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watch error")
                await asyncio.sleep(1)
            await asyncio.sleep(0.1)

    async def _guard_links(self, nodes):
        """While the adapter's audio nodes exist, keep them isolated."""
        now = time.monotonic()
        if nodes and nodes != self._guarded:
            # new call: attach its streams to our internal (clocked) nodes
            await isolate(await bluez_nodes(self.bt.mac, with_ids=True))
        if nodes and (nodes != self._guarded or now - self._guard_t > 0.5):
            self._guard_t = now
            if await unlink_foreign(self.bt.mac):
                log.debug("cut foreign links to the adapter")
        self._guarded = nodes

    async def _watch_once(self, last_check):
        now = time.monotonic()
        if not self.bt:
            await asyncio.sleep(0.5)
            return last_check
        if self.state == self.BUSY:          # a test drives the adapter
            await self._guard_links(await bluez_nodes(self.bt.mac))
            await asyncio.sleep(0.1)
            return last_check
        if now - last_check > 5 and self.state == self.IDLE:
            last_check = now
            up = await self.bt.connected()
            if not up:
                if self.link_up:
                    self.event("adapter disconnected", logging.WARNING)
                up = await self.bt.connect()
                if up:
                    self.event("adapter connected")
            elif not self.link_up:
                self.event("adapter connected")
            self.link_up = up
        self.nodes = await bluez_nodes(self.bt.mac)
        await self._guard_links(self.nodes)
        if self.state == self.IDLE and now >= self.rx_blocked_until:
            inp = next((n for n in self.nodes if "input" in n), None)
            if inp:
                self.rx_task = asyncio.get_running_loop().create_task(
                    self.do_rx(inp))
        return last_check

    # --- maintenance (pair, loop test) without stopping the service
    async def maintenance(self, what, coro):
        if self.state not in (self.IDLE,):
            raise RuntimeError(f"busy ({self.state})")
        self.state = self.BUSY
        self.event(f"{what}: start")
        try:
            return await coro
        finally:
            self.state = self.IDLE
            self.event(f"{what}: end")

    async def pair(self, timeout=120):
        from .bt import pair as bt_pair
        from .cli import write_config

        def say(m):
            asyncio.run_coroutine_threadsafe(
                self._say(m), loop)
        loop = asyncio.get_running_loop()
        mac = await self.maintenance(
            "pair", asyncio.to_thread(bt_pair, timeout, "", say))
        if mac:
            self.bt = Adapter(mac)
            self.link_up = False
            self.cfg.set("bluetooth", "mac", mac)
            if self.cfg_path:
                write_config(self.cfg_path, mac)
        return mac

    async def _say(self, m):
        self.event(m)

    async def loop_test(self, out_path, pcm=None):
        from .selftest import loop_test, test_signal
        ok = await self.maintenance(
            "test", loop_test(self.bt.mac, pcm or test_signal(), out_path,
                              say=self.event))
        return ok

    # --- main
    async def run(self, web=None):
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)
        self.internal = InternalNodes()
        await self.internal.start()
        for link in self.links:
            await link.start()
        if web:
            await web.start(self)
        self.event("k-link started" + (f", adapter {self.bt.mac}" if self.bt
                                       else ", no adapter paired yet"))
        watcher = loop.create_task(self.watch())
        await stop.wait()
        self.event("stopping")
        watcher.cancel()
        if self.tx_task and not self.tx_task.done():
            self.tx_task.cancel()        # its finally: unkeys the radio
            await asyncio.gather(self.tx_task, return_exceptions=True)
        for link in self.links:
            await link.stop()
        await self.internal.stop()
