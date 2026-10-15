#!/usr/bin/env python3
"""USRP test peer for k-link: send a WAV to transmit and record what comes back.

  usrp_test.py --send msg.wav --record out.wav --wait 20
"""
import argparse, socket, struct, time, wave, subprocess, os, tempfile

HDR = struct.Struct(">4sIIIIIII")

def load(path):
    # any WAV -> 8 kHz mono s16 via sox
    tmp = tempfile.mktemp(suffix=".raw")
    subprocess.run(["sox", path, "-r", "8000", "-c", "1", "-b", "16",
                    "-e", "signed", "-t", "raw", tmp], check=True)
    data = open(tmp, "rb").read(); os.unlink(tmp)
    return data

ap = argparse.ArgumentParser()
ap.add_argument("--klink", default="127.0.0.1:34001")
ap.add_argument("--listen", type=int, default=32001)
ap.add_argument("--send")
ap.add_argument("--record")
ap.add_argument("--wait", type=float, default=15)
a = ap.parse_args()
host, port = a.klink.split(":")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", a.listen))
seq = 0
if a.send:
    pcm = load(a.send)
    t = time.monotonic()
    for i in range(0, len(pcm) - 319, 320):
        s.sendto(HDR.pack(b"USRP", seq, 0, 1, 0, 0, 0, 0) + pcm[i:i+320], (host, int(port)))
        seq += 1; t += 0.02
        time.sleep(max(0, t - time.monotonic()))
    s.sendto(HDR.pack(b"USRP", seq, 0, 0, 0, 0, 0, 0), (host, int(port)))
    print(f"sent {seq} frames ({seq*0.02:.1f} s)")
out = bytearray(); keys = 0; unkeys = 0
s.settimeout(0.5); end = time.monotonic() + a.wait
while time.monotonic() < end:
    try:
        d, _ = s.recvfrom(2048)
    except socket.timeout:
        continue
    if d[:4] != b"USRP": continue
    keyup = HDR.unpack_from(d)[3]
    if keyup: keys += 1; out += d[32:]
    else: unkeys += 1; print(f"{time.strftime('%T')} unkey")
print(f"received {keys} voice frames ({keys*0.02:.1f} s), {unkeys} unkeys")
if a.record and out:
    w = wave.open(a.record, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
    w.writeframes(bytes(out)); w.close()
