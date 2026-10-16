"""Built-in web UI: status, levels, pairing, loop test and settings.

Plain asyncio HTTP (no dependencies). Live updates use Server-Sent Events.
Protected with HTTP basic auth ([web] password); with no password set it
only listens on localhost.
"""
import asyncio
import base64
import configparser
import hmac
import json
import logging
import os
import sys
import time
from importlib import resources
from pathlib import Path

from .audio import FRAME_BYTES

log = logging.getLogger("k-link")

# settings the UI may edit: section -> {key: type}
EDITABLE = {
    "usrp": {"enabled": bool, "listen_port": int, "remote_host": str,
             "remote_port": int, "talkgroup": int},
    "soundcard": {"enabled": bool, "control": ("vox", "serial"),
                  "vox_threshold_db": float, "vox_hang": float,
                  "vox_preroll": float, "serial_ptt_pin": ("rts", "dtr")},
    "ptt": {"tx_timeout": float, "net_hang": float,
            "rx_ignore_after_tx": float},
    "web": {"port": int},
}


class BeepLink:
    """Transmits the test beeps from the UI's PTT button."""
    name = "web"

    def __init__(self, gw):
        self.gw = gw

    async def send(self, pcm):
        loop = asyncio.get_running_loop()
        t = loop.time()
        for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            self.gw.tx_frame(self, True, pcm[i:i + FRAME_BYTES])
            t += 0.02
            await asyncio.sleep(max(0, t - loop.time()))
        self.gw.tx_frame(self, False, b"")


class Web:
    def __init__(self, cfg, cfg_path):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.password = cfg.get("web", "password", fallback="").strip()
        self.port = cfg.getint("web", "port", fallback=8077)
        self.host = cfg.get("web", "listen_host",
                            fallback="0.0.0.0" if self.password
                            else "127.0.0.1")
        self.gw = None
        self.test_wav = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / \
            "k-link-test.wav"
        self.page = resources.files("klink").joinpath(
            "web/index.html").read_bytes()

    async def start(self, gw):
        self.gw = gw
        self.beep = BeepLink(gw)
        await asyncio.start_server(self.handle, self.host, self.port)
        where = self.host if self.host != "0.0.0.0" else "<this host>"
        log.info("web UI on http://%s:%d/%s", where, self.port,
                 "" if self.password else " (localhost only: no password)")

    # --- HTTP plumbing
    async def handle(self, r, w):
        try:
            line = (await r.readline()).decode(errors="replace").split()
            if len(line) < 2:
                return
            method, path = line[0], line[1].split("?")[0]
            headers = {}
            while True:
                h = (await r.readline()).decode(errors="replace").strip()
                if not h:
                    break
                k, _, v = h.partition(":")
                headers[k.lower()] = v.strip()
            body = b""
            n = int(headers.get("content-length", 0) or 0)
            if n:
                body = await r.readexactly(min(n, 65536))
            if not self.authorized(headers):
                return await self.send(w, 401, b"login required",
                                       extra={"WWW-Authenticate":
                                              'Basic realm="k-link"'})
            await self.route(method, path, body, w)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("web error")
        finally:
            try:
                w.close()
            except Exception:
                pass

    def authorized(self, headers):
        if not self.password:
            return True                 # bound to localhost only
        a = headers.get("authorization", "")
        if not a.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(a[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(pw, self.password)

    async def send(self, w, code, data, ctype="text/plain; charset=utf-8",
                   extra=None):
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found", 409: "Conflict"}.get(code, "OK")
        hdr = [f"HTTP/1.1 {code} {reason}", f"Content-Type: {ctype}",
               f"Content-Length: {len(data)}", "Cache-Control: no-store",
               "Connection: close"]
        for k, v in (extra or {}).items():
            hdr.append(f"{k}: {v}")
        w.write(("\r\n".join(hdr) + "\r\n\r\n").encode() + data)
        await w.drain()

    async def json(self, w, obj, code=200):
        await self.send(w, code, json.dumps(obj).encode(), "application/json")

    # --- routes
    async def route(self, method, path, body, w):
        gw = self.gw
        if method == "GET" and path == "/":
            return await self.send(w, 200, self.page,
                                   "text/html; charset=utf-8")
        if method == "GET" and path == "/api/status":
            return await self.json(w, gw.status())
        if method == "GET" and path == "/api/events":
            return await self.sse(w)
        if method == "GET" and path == "/api/config":
            return await self.json(w, self.config_view())
        if method == "GET" and path == "/api/test.wav":
            if not self.test_wav.exists():
                return await self.send(w, 404, b"no test yet")
            return await self.send(w, 200, self.test_wav.read_bytes(),
                                   "audio/wav")
        if method == "POST" and path == "/api/config":
            return await self.save_config(body, w)
        if method == "POST" and path in ("/api/pair", "/api/test",
                                         "/api/beep"):
            if gw.state != gw.IDLE:
                return await self.json(w, {"error": f"busy ({gw.state})"},
                                       409)
            if path != "/api/pair" and not gw.bt:
                return await self.json(w, {"error": "pair first"}, 409)
            loop = asyncio.get_running_loop()
            if path == "/api/pair":
                loop.create_task(self._bg(gw.pair()))
            elif path == "/api/test":
                loop.create_task(self._bg(gw.loop_test(str(self.test_wav))))
            else:
                from .selftest import test_signal
                loop.create_task(self._bg(self.beep.send(test_signal())))
            return await self.json(w, {"ok": True})
        await self.send(w, 404, b"not found")

    async def _bg(self, coro):
        try:
            await coro
        except Exception as e:
            log.exception("web action failed")
            self.gw.event(f"error: {e!r}", logging.ERROR)

    async def sse(self, w):
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n")
        q = asyncio.Queue(maxsize=100)
        self.gw.listeners.add(q)
        try:
            for e in list(self.gw.events)[-50:]:
                w.write(b"event: event\ndata: " + json.dumps(e).encode()
                        + b"\n\n")
            while True:
                try:
                    kind, data = await asyncio.wait_for(q.get(), 0.2)
                    w.write(f"event: {kind}\ndata: ".encode()
                            + json.dumps(data).encode() + b"\n\n")
                except asyncio.TimeoutError:
                    pass
                w.write(b"event: status\ndata: "
                        + json.dumps(self.gw.status()).encode() + b"\n\n")
                await w.drain()
        finally:
            self.gw.listeners.discard(q)

    # --- settings
    def config_view(self):
        out = {}
        for sec, keys in EDITABLE.items():
            out[sec] = {}
            for k, typ in keys.items():
                v = self.cfg.get(sec, k, fallback=None)
                if v is None:
                    continue
                if typ is bool:
                    v = self.cfg.getboolean(sec, k)
                out[sec][k] = v
        out["soundcard"].setdefault("control", "vox")
        out["soundcard"].setdefault("serial_ptt_pin", "rts")
        out["bluetooth"] = {"mac": self.cfg.get("bluetooth", "mac",
                                                fallback="")}
        out["_host_port"] = self.port
        return out

    async def save_config(self, body, w):
        try:
            new = json.loads(body)
            cp = configparser.ConfigParser()
            cp.read(self.cfg_path)
            for sec, vals in new.items():
                if sec not in EDITABLE:
                    continue
                if not cp.has_section(sec):
                    cp.add_section(sec)
                for k, v in vals.items():
                    typ = EDITABLE[sec].get(k)
                    if typ is None:
                        continue
                    if typ is bool:
                        v = "yes" if v in (True, "true", "yes", "1") else "no"
                    elif isinstance(typ, tuple):
                        if v not in typ:
                            raise ValueError(f"{sec}.{k}: {v!r}")
                    else:
                        v = str(typ(v))
                    cp.set(sec, k, v)
        except (ValueError, TypeError) as e:
            return await self.json(w, {"error": str(e)}, 400)
        with open(self.cfg_path, "w") as f:
            cp.write(f)
        await self.json(w, {"ok": True, "restarting": True})
        self.gw.event("settings saved, restarting")
        asyncio.get_running_loop().call_later(0.5, self.restart)

    @staticmethod
    def restart():
        os.execv(sys.executable, [sys.executable, "-m", "klink"]
                 + sys.argv[1:])
