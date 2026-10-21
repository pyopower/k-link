#!/usr/bin/python3
"""k-link node web panel (root, 127.0.0.1:8078 behind Apache).

Status, link/unlink, settings, wifi and reboot (SD image); in Docker the
settings come from docker-compose.yml and are shown read-only.
Standard library only."""
import base64
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, "/usr/lib/k-link-node")
import nodecfg  # noqa: E402

HERE = Path(__file__).parent
PORT = int(os.environ.get("PANEL_PORT") or nodecfg.load().get("PANEL_PORT") or 8078)
job = {"busy": False, "msg": ""}


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def ast(cmd):
    out = sh(["asterisk", "-rx", cmd])
    return "\n".join(ln for ln in out.splitlines()
                     if "global EID" not in ln).strip()


def active(unit):
    if nodecfg.DOCKER:
        return "RUNNING" in sh(["supervisorctl", "status", unit])
    return sh(["systemctl", "is-active", unit]) == "active"


def status():
    cfg = nodecfg.load()
    node = nodecfg.node_number(cfg)
    st = {"hostname": socket.gethostname(), "node": node,
          "private": node == nodecfg.PRIVATE_NODE,
          "default_password": cfg["ADMIN_PASSWORD"] == "k-link",
          "job": job, "docker": nodecfg.DOCKER,
          "klink_port": cfg.get("KLINK_WEB_PORT", "8077")}
    st["ips"] = sh(["hostname", "-I"]).split()
    wl = sh(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"])
    st["wifi"] = next((ln.split(":")[1:] for ln in wl.splitlines()
                       if ln.startswith("yes:")), None)
    st["ap"] = "k-link-ap" in sh(["nmcli", "-t", "-f", "NAME", "con",
                                  "show", "--active"]).splitlines()
    try:
        st["temp"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp")
                               .read_text()) / 1000, 1)
    except OSError:
        st["temp"] = None
    mem = dict(re.findall(r"(\w+):\s+(\d+)", Path("/proc/meminfo").read_text()))
    st["mem"] = [int(mem["MemTotal"]) - int(mem["MemAvailable"]),
                 int(mem["MemTotal"])]
    st["uptime"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    st["asterisk"] = active("asterisk")
    st["klink"] = nodecfg.klink_active()
    if st["asterisk"]:
        st["links"] = ast(f"rpt lstats {node}")
        st["nodes"] = ast(f"rpt nodes {node}")
        st["registration"] = ast("rpt show registrations")
        st["echolink"] = "chan_echolink" in ast("module show like echolink")
    return st


def apply_job(cfg):
    job.update(busy=True, msg="Aplicando ajustes…")
    try:
        nodecfg.save(cfg)
        nodecfg.apply_all(cfg)
        job["msg"] = "Ajustes aplicados."
    except Exception as e:  # noqa: BLE001
        job["msg"] = f"Error: {e}"
    job["busy"] = False


def wifi_job():
    job.update(busy=True, msg="Probando wifi… (si falla vuelve el punto de acceso)")
    out = sh(["k-link-node-wifi", "try"], timeout=90)
    job.update(busy=False, msg=f"Wifi: {out or 'sin respuesta'}")


def background(fn, *a):
    if job["busy"]:
        return False
    threading.Thread(target=fn, args=a, daemon=True).start()
    return True


class H(BaseHTTPRequestHandler):
    server_version = "k-link-node"

    def log_message(self, fmt, *a):
        pass

    def authed(self):
        pw = nodecfg.load()["ADMIN_PASSWORD"]
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, got = base64.b64decode(h[6:]).decode().partition(":")
                if hmac.compare_digest(got.encode(), pw.encode()):
                    return True
            except ValueError:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="k-link node"')
        self.end_headers()
        return False

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json"
                    else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authed():
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.send(200, (HERE / "index.html").read_bytes(), "text/html")
        elif path == "/api/status":
            self.send(200, status())
        elif path == "/api/config":
            cfg = nodecfg.load()
            self.send(200, {k: ("" if "PASSWORD" in k and k != "ADMIN_PASSWORD"
                                and cfg[k] else cfg[k]) for k in cfg} |
                      {"_set": [k for k in cfg if "PASSWORD" in k and cfg[k]]})
        elif path == "/api/scan":
            out = sh(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev",
                      "wifi", "list", "--rescan", "auto"], timeout=20)
            nets = {}
            for ln in out.splitlines():
                p = re.split(r"(?<!\\):", ln)
                if len(p) >= 3 and p[0]:
                    ssid = p[0].replace("\\:", ":")
                    nets[ssid] = max(nets.get(ssid, 0), int(p[1] or 0))
            self.send(200, sorted(nets.items(), key=lambda x: -x[1]))
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.authed():
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send(403, {"error": "origin"})
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        path = urlsplit(self.path).path
        cfg = nodecfg.load()
        node = nodecfg.node_number(cfg)

        if nodecfg.DOCKER and path in ("/api/config", "/api/wifi",
                                       "/api/reboot", "/api/poweroff"):
            return self.send(400, {"error": ["Docker: cambia los ajustes en "
                                              "docker-compose.yml y recrea el contenedor"]})

        if path == "/api/config":
            new = dict(cfg)
            for k in nodecfg.DEFAULTS:
                if k in data:
                    v = str(data[k]).strip()
                    if "PASSWORD" in k and not v:
                        continue       # empty password field = keep it
                    new[k] = v
            new["ECHOLINK"] = "yes" if new.get("ECHOLINK") == "yes" else "no"
            err = nodecfg.validate(new)
            if err:
                return self.send(400, {"error": err})
            if not background(apply_job, new):
                return self.send(409, {"error": ["Hay otra tarea en marcha"]})
            return self.send(200, {"ok": True})

        if path == "/api/link":
            target = str(data.get("node", ""))
            if not target.isdigit():
                return self.send(400, {"error": "nodo no válido"})
            mode = {"tx": 3, "mon": 2, "off": 1}.get(data.get("mode"), 3)
            return self.send(200, {"out": ast(f"rpt cmd {node} ilink {mode} {target}")})

        if path == "/api/unlinkall":
            return self.send(200, {"out": ast(f"rpt cmd {node} ilink 6 0")})

        if path == "/api/wifi":
            ssid = str(data.get("ssid", "")).strip()
            if not ssid:
                return self.send(400, {"error": "falta el nombre de la red"})
            ok, msg = nodecfg.add_wifi(ssid, str(data.get("password", "")))
            if not ok:
                return self.send(400, {"error": msg})
            background(wifi_job)
            return self.send(200, {"ok": True})

        if path in ("/api/reboot", "/api/poweroff"):
            self.send(200, {"ok": True})
            threading.Timer(1, lambda: sh(["systemctl", path[5:]])).start()
            return

        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.serve_forever()
