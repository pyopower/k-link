"""Loop test: transmit a signal and record what the radio hears back.

Meant for a radio sitting on an echo/parrot channel (e.g. DMR TG 9990):
TX -> parrot -> RX proves PTT, both audio paths and the RX detection.
"""
import asyncio
import math
import shutil
import struct
import subprocess
import time
import wave

from .audio import RATE, bluez_nodes, pw_cat, stop_proc
from .bt import Adapter


def test_signal():
    """~4 s of beeps (alternating 800/1200 Hz, 0.4 s on, 0.2 s off)."""
    out = bytearray()
    for i in range(7):
        f = 800 if i % 2 == 0 else 1200
        for n in range(int(0.4 * RATE)):
            env = min(1.0, n / 200, (0.4 * RATE - n) / 200)
            out += struct.pack("<h", int(12000 * env *
                                         math.sin(2 * math.pi * f * n / RATE)))
        out += bytes(int(0.2 * RATE) * 2)
    return bytes(out)


def load_wav(path):
    """Any WAV as 8 kHz mono s16 (sox converts it when available)."""
    if shutil.which("sox"):
        return subprocess.run(
            ["sox", path, "-r", str(RATE), "-c", "1", "-b", "16",
             "-e", "signed", "-t", "raw", "-"],
            capture_output=True, check=True).stdout
    with wave.open(path) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != \
                (RATE, 1, 2):
            raise SystemExit("without sox the WAV must be 8 kHz mono 16-bit")
        return w.readframes(w.getnframes())


def level(pcm):
    a = struct.unpack("<%dh" % (len(pcm) // 2), pcm[:len(pcm) & ~1])
    if not a:
        return -99.0, -99.0
    peak = max(abs(x) for x in a) or 1
    rms = math.sqrt(sum(x * x for x in a) / len(a)) or 1
    return 20 * math.log10(peak / 32768), 20 * math.log10(rms / 32768)


async def node(mac, kind, wait):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        n = next((x for x in await bluez_nodes(mac) if kind in x), None)
        if n:
            return n
        await asyncio.sleep(0.05)
    return None


async def loop_test(mac, pcm, out_path, wait=20, say=print):
    bt = Adapter(mac)
    if not await bt.connected():
        say("connecting to the adapter...")
        if not await bt.connect():
            say("FAIL: cannot connect to " + mac)
            return False
        await asyncio.sleep(3)
    sent = len(pcm) / 2 / RATE
    say(f"TX {sent:.1f} s ...")
    await bt.ptt_down()
    try:
        out = await node(mac, "output", 3)
        if not out:
            say("FAIL: the adapter did not open the TX audio (PTT not taken?)")
            return False
        await asyncio.sleep(0.5)          # let the radio settle into TX
        player = await pw_cat("play", out)
        t_play = time.monotonic()
        player.stdin.write(pcm)
        await player.stdin.drain()
        player.stdin.close()
        # pw-play does not always exit at EOF: give it the audio's duration
        # (plus its latency) and stop it ourselves
        try:
            await asyncio.wait_for(
                player.wait(), max(0.1, sent + 0.5 - (time.monotonic()
                                                      - t_play)))
        except asyncio.TimeoutError:
            await stop_proc(player)
    finally:
        await bt.ptt_up()
    t_unkey = time.monotonic()
    await asyncio.sleep(1.5)              # skip the adapter's post-TX tail
    say(f"listening up to {wait} s for the echo ...")
    got = bytearray()
    first = None
    last_end = None
    end = t_unkey + wait
    # the adapter closes its RX call on every pause, so the echo may come in
    # several pieces: collect them until 3 s pass with nothing new. Short
    # calls (the adapter's ~1 s post-TX tail) don't count as the echo.
    while time.monotonic() < end:
        if last_end and time.monotonic() - last_end > 3:
            break
        inp = next((x for x in await bluez_nodes(mac) if "input" in x), None)
        if not inp:
            await asyncio.sleep(0.1)
            continue
        t_seg = time.monotonic()
        seg = bytearray()
        rec = await pw_cat("rec", inp)
        while time.monotonic() < end:
            try:
                seg += await asyncio.wait_for(rec.stdout.read(3200), 0.2)
            except asyncio.TimeoutError:
                pass
            if inp not in await bluez_nodes(mac):
                break
        await stop_proc(rec)
        tail = t_seg - t_unkey < 2.5 and time.monotonic() - t_seg < 1.5
        if not tail:
            first = first or t_seg - t_unkey
            got += seg[:len(seg) & ~1]      # whole samples only
            last_end = time.monotonic()
    if not got:
        say("FAIL: nothing received (is the radio on an echo channel?)")
        return False
    dur = len(got) / 2 / RATE
    peak, rms = level(bytes(got))
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(got))
    say(f"RX {dur:.1f} s, {first:.1f} s after unkey; "
        f"peak {peak:.1f} dBFS, rms {rms:.1f} dBFS -> {out_path}")
    if peak > -1:
        say("warning: RX clipping, turn the radio volume down")
    elif peak < -30:
        say("warning: RX very low, turn the radio volume up")
    ok = dur >= sent * 0.7
    say("PASS" if ok else "FAIL: echo much shorter than what was sent")
    return ok
