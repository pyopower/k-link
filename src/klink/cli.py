"""k-link command line: run | pair | test | status | config | install-service."""
import argparse
import asyncio
import configparser
import fcntl
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

CONFIG_PATHS = [Path("k-link.ini"),
                Path.home() / ".config/k-link/k-link.ini",
                Path("/etc/k-link/k-link.ini")]

DEFAULT_CONFIG = """\
[bluetooth]
# K-plug adapter MAC (k-link pair writes it here)
mac =

[usrp]
# k-link listens here for audio to transmit...
listen_host = 0.0.0.0
listen_port = 34001
# ...and sends what the radio receives here
# (ASL: rxchannel = USRP/<this host>:34001:<remote_port>)
remote_host = 127.0.0.1
remote_port = 32001
talkgroup = 0

[ptt]
# hard limit for one transmission, seconds
tx_timeout = 180
# unkey when the network goes quiet this long without an explicit unkey
net_hang = 0.5
# after unkeying the adapter opens a ~1 s "call" (tail): don't take it as RX
rx_ignore_after_tx = 1.5
"""

UNIT = """\
[Unit]
Description=k-link radio gateway (Bluetooth K-plug adapter <-> USRP)
After=pipewire.service wireplumber.service
Wants=pipewire.service wireplumber.service

[Service]
ExecStart={exe} -c {config} run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def find_config(path):
    if path:
        return Path(path)
    for p in CONFIG_PATHS:
        if p.exists():
            return p
    return None


def load_config(path):
    p = find_config(path)
    if not p:
        sys.exit("no config found; create one with: k-link config")
    cfg = configparser.ConfigParser()
    cfg.read(p)
    if not cfg.get("bluetooth", "mac", fallback=""):
        sys.exit(f"{p}: no adapter MAC yet; run: k-link pair")
    return p, cfg


def lock():
    """Only one k-link may drive the adapter (service or test)."""
    d = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    f = open(Path(d) / "k-link.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("k-link is already running (stop the service first)")
    return f


def write_config(path, mac=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text()
    else:
        text = DEFAULT_CONFIG
    if mac:
        lines = text.splitlines()
        lines = [f"mac = {mac}" if ln.strip().startswith("mac") else ln
                 for ln in lines]
        text = "\n".join(lines) + "\n"
    path.write_text(text)


def cmd_run(a):
    from .gateway import Gateway
    path, cfg = load_config(a.config)
    _lock = lock()
    logging.getLogger("k-link").info("config %s", path)
    asyncio.run(Gateway(cfg).run())


def cmd_pair(a):
    from .bt import pair
    _lock = lock()
    mac = pair(timeout=a.timeout, mic_name=a.as_name)
    if not mac:
        sys.exit(1)
    path = find_config(a.config) or CONFIG_PATHS[1]
    write_config(path, mac)
    print(f"saved to {path}")


def cmd_test(a):
    from .selftest import loop_test, load_wav, test_signal
    path, cfg = load_config(a.config)
    _lock = lock()
    pcm = load_wav(a.wav) if a.wav else test_signal()
    ok = asyncio.run(loop_test(cfg.get("bluetooth", "mac"), pcm, a.out,
                               wait=a.wait))
    sys.exit(0 if ok else 1)


def cmd_status(a):
    from .bt import Adapter
    from .gateway import bluez_nodes
    path, cfg = load_config(a.config)
    mac = cfg.get("bluetooth", "mac")

    async def st():
        bt = Adapter(mac)
        return await bt.connected(), await bluez_nodes(mac)
    conn, nodes = asyncio.run(st())
    print(f"config    {path}")
    print(f"adapter   {mac} {'connected' if conn else 'NOT connected'}")
    print(f"audio     {', '.join(sorted(nodes)) or 'no call open (idle)'}")
    print(f"usrp      listen :{cfg.get('usrp', 'listen_port')} -> "
          f"{cfg.get('usrp', 'remote_host')}:{cfg.get('usrp', 'remote_port')}")
    svc = subprocess.run(["systemctl", "--user", "is-active", "k-link"],
                         capture_output=True, text=True).stdout.strip()
    print(f"service   {svc or 'unknown'}")


def cmd_config(a):
    path = Path(a.config) if a.config else CONFIG_PATHS[1]
    if path.exists():
        print(f"{path} already exists")
    else:
        write_config(path)
        print(f"created {path}")


def cmd_install_service(a):
    path = find_config(a.config)
    if not path:
        sys.exit("no config found; run: k-link config && k-link pair")
    exe = shutil.which("k-link") or f"{sys.executable} -m klink"
    unit = Path.home() / ".config/systemd/user/k-link.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(UNIT.format(exe=exe, config=path.resolve()))
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "--now", "k-link"])
    print(f"installed {unit}")
    user = os.environ.get("USER", "")
    linger = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"],
                            capture_output=True, text=True).stdout
    if "Linger=yes" not in linger:
        print("to start at boot without logging in, run once:\n"
              f"  sudo loginctl enable-linger {user}")


def main():
    ap = argparse.ArgumentParser(
        prog="k-link",
        description="Radio gateway through a Bluetooth K-plug PTT adapter.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-c", "--config", help="config file (default: ./k-link.ini,"
                    " ~/.config/k-link/k-link.ini, /etc/k-link/k-link.ini)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the gateway").set_defaults(f=cmd_run)
    p = sub.add_parser("pair", help="pair the K-plug adapter")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--as-name", default="KST_vHMIC010",
                   help="Bluetooth name of the speaker-mic to impersonate "
                   "(empty: keep this computer's own name)")
    p.set_defaults(f=cmd_pair)
    p = sub.add_parser("test", help="loop test through an echo channel")
    p.add_argument("--wav", help="WAV to transmit (default: test beeps)")
    p.add_argument("--out", default="k-link-test.wav")
    p.add_argument("--wait", type=float, default=20)
    p.set_defaults(f=cmd_test)
    sub.add_parser("status", help="show adapter and service state"
                   ).set_defaults(f=cmd_status)
    sub.add_parser("config", help="create a default config"
                   ).set_defaults(f=cmd_config)
    sub.add_parser("install-service", help="install the systemd user service"
                   ).set_defaults(f=cmd_install_service)
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    a.f(a)


if __name__ == "__main__":
    main()
