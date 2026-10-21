"""Settings of a k-link node and how they turn into ASL / EchoLink / k-link
configuration. Shared by the SD image and the Docker image.

KLN_MODE=image  (default) settings in /etc/k-link-node/node.conf, editable
                from the web panel; k-link runs as user service of uid 1000.
KLN_MODE=docker settings come from environment variables (docker-compose.yml)
                and are applied at every container start; k-link runs as root
                with its config in /data/k-link.ini.
"""
import os
import re
import secrets
import subprocess
from pathlib import Path

MODE = os.environ.get("KLN_MODE", "image")
DOCKER = MODE == "docker"
CONF = Path("/etc/k-link-node/node.conf")
SECRET = Path("/data/manager.secret" if DOCKER
              else "/etc/k-link-node/manager.secret")
AST = Path("/etc/asterisk")
BOOT_FILE = Path("/boot/firmware/k-link-node.txt")
PRIVATE_NODE = "1999"

DEFAULTS = {
    "ADMIN_PASSWORD": "k-link", "WIFI_COUNTRY": "ES", "CALLSIGN": "",
    "NODE": "", "NODE_PASSWORD": "", "AUTOLINK": "",
    "ECHOLINK": "no", "EL_CALL": "", "EL_PASSWORD": "", "EL_NODE": "",
    "EL_NAME": "", "EL_QTH": "", "EL_EMAIL": "",
    "AP_SSID": "K-LINK-SETUP", "AP_PASSWORD": "k-link-setup",
}
# only through environment variables (Docker)
EXTRA = {"IAX_PORT": "4569", "WEB_PORT": "80", "KLINK_WEB_PORT": "8077",
         "PANEL_PORT": "8078", "USRP_KLINK_PORT": "34001",
         "USRP_ASL_PORT": "32001", "BT_MAC": "", "DASH_TITLE": ""}

# docker-compose variable -> setting (names close to the usual ASL composes)
ENV_MAP = {
    "ADMIN_PASSWORD": "ADMIN_PASSWORD", "CALLSIGN": "CALLSIGN",
    "ASLNODE": "NODE", "ASLPASS": "NODE_PASSWORD",
    "ECHOLINKCALLSIGN": "EL_CALL", "ECHOLINKNODE": "EL_NODE",
    "ECHOLINKPASS": "EL_PASSWORD", "ECHOLINKNAME": "EL_NAME",
    "ECHOLINKQTH": "EL_QTH", "ECHOLINKEMAIL": "EL_EMAIL",
}


def parse(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip().upper()] = v
    return out


def from_env(env=os.environ):
    cfg = dict(DEFAULTS) | dict(EXTRA)
    for var, key in ENV_MAP.items():
        if env.get(var, "").strip():
            cfg[key] = env[var].strip()
    for key in EXTRA:
        if env.get(key, "").strip():
            cfg[key] = env[key].strip()
    # auto connect: AC_ASL=2000,2001  AC_ECHOLINK=123456
    links = [n for n in re.split(r"[,\s]+", env.get("AC_ASL", "")) if n]
    links += ["3" + n.zfill(6) for n in re.split(r"[,\s]+",
                                                 env.get("AC_ECHOLINK", "")) if n]
    cfg["AUTOLINK"] = ",".join(links)
    cfg["ECHOLINK"] = "yes" if cfg["EL_NODE"] and cfg["EL_PASSWORD"] else "no"
    return cfg


def load():
    if DOCKER:
        return from_env()
    cfg = dict(DEFAULTS) | dict(EXTRA)
    if CONF.exists():
        cfg.update(parse(CONF.read_text()))
    return cfg


def save(cfg):
    if DOCKER:
        raise RuntimeError("Docker: settings come from docker-compose.yml")
    CONF.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# k-link node settings (edit from the web panel)"]
    for k in DEFAULTS:
        v = str(cfg.get(k, "")).replace("\n", " ")
        lines.append(f"{k}={v}")
    tmp = CONF.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONF)


def node_number(cfg):
    n = cfg.get("NODE", "").strip()
    return n if n.isdigit() else PRIVATE_NODE


def autolinks(cfg):
    return [n for n in re.split(r"[,\s]+", cfg.get("AUTOLINK", "")) if n]


def validate(cfg):
    """Return a list of problems (in Spanish, shown in the web panel)."""
    err = []
    for k in ("NODE", "EL_NODE", "IAX_PORT", "WEB_PORT", "KLINK_WEB_PORT",
              "PANEL_PORT", "USRP_KLINK_PORT", "USRP_ASL_PORT"):
        if cfg.get(k) and not cfg[k].isdigit():
            err.append(f"{k}: solo cifras")
    if any(not n.isdigit() for n in autolinks(cfg)):
        err.append("AUTOLINK: nodos separados por comas, solo cifras")
    if cfg.get("CALLSIGN") and not re.fullmatch(r"[A-Za-z0-9/]{3,12}",
                                                cfg["CALLSIGN"]):
        err.append("CALLSIGN: indicativo no válido")
    if cfg.get("BT_MAC") and not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}",
                                              cfg["BT_MAC"]):
        err.append("BT_MAC: formato AA:BB:CC:DD:EE:FF")
    if len(cfg.get("ADMIN_PASSWORD", "")) < 4:
        err.append("ADMIN_PASSWORD: mínimo 4 caracteres")
    if not DOCKER and len(cfg.get("AP_PASSWORD", "")) < 8:
        err.append("AP_PASSWORD: mínimo 8 caracteres (WPA2)")
    if cfg.get("ECHOLINK") == "yes":
        for k in ("EL_CALL", "EL_PASSWORD", "EL_NODE"):
            if not cfg.get(k):
                err.append(f"{k}: necesario para EchoLink")
    for k, v in cfg.items():
        if any(c in str(v) for c in "\n\r;[]"):
            err.append(f"{k}: caracteres no permitidos (; [ ])")
    return err


# ---------------------------------------------------------------- asterisk

def _set_key(text, key, value, comment_out=False):
    """Set 'key = value' (first occurrence, commented or not)."""
    pat = re.compile(rf"^\s*;?\s*{re.escape(key)}\s*=.*$", re.M)
    new = f"{';' if comment_out else ''}{key} = {value}"
    if pat.search(text):
        return pat.sub(new.replace("\\", "\\\\"), text, count=1)
    return text + new + "\n"


def _module(text, mod, load_it):
    pat = re.compile(rf"^\s*(no)?load\s*=\s*{re.escape(mod)}.*$", re.M)
    line = f"{'load' if load_it else 'noload'}  = {mod}"
    return pat.sub(line, text, count=1) if pat.search(text) else text + line + "\n"


def _write(path, text):
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def manager_secret():
    if not SECRET.exists():
        SECRET.parent.mkdir(parents=True, exist_ok=True)
        SECRET.write_text(secrets.token_urlsafe(18))
        os.chmod(SECRET, 0o600)
    return SECRET.read_text().strip()


def apply_asterisk(cfg):
    node = node_number(cfg)
    call = cfg.get("CALLSIGN", "").upper()
    kport, aport = cfg.get("USRP_KLINK_PORT", "34001"), cfg.get("USRP_ASL_PORT", "32001")

    p = AST / "rpt.conf"
    t = p.read_text()
    t = re.sub(r"^\d+ = radio@127\.0\.0\.1/\d+,NONE$",
               f"{node} = radio@127.0.0.1/{node},NONE", t, count=1, flags=re.M)
    # our node stanza is always the last thing in the file
    t = re.split(r"^\[\d+\]\(node-main\).*$", t, flags=re.M)[0].rstrip() + "\n"
    stanza = [f"\n[{node}](node-main)",
              f"rxchannel = USRP/127.0.0.1:{kport}:{aport}"
              "   ; k-link (radio through the Bluetooth K-plug adapter)",
              "duplex = 1                ; simplex radio: no repeat",
              "hangtime = 500", "althangtime = 500",
              "linkunkeyct = ct8"]
    if call:
        stanza += [f"idrecording = |i{call}", "idtime = 540000"]
    else:
        stanza += ["idrecording = |i", "idtime = 0"]
    if autolinks(cfg):
        stanza.append("startup_macro = " +
                      "".join(f"*3{n}" for n in autolinks(cfg)))
    t += "\n".join(stanza) + "\n"
    _write(p, t)

    p = AST / "modules.conf"
    t = p.read_text()
    t = _module(t, "chan_usrp.so", True)
    t = _module(t, "chan_simpleusb.so", False)
    t = _module(t, "chan_echolink.so", cfg.get("ECHOLINK") == "yes")
    _write(p, t)

    p = AST / "echolink.conf"
    t = p.read_text()
    for k, key in (("EL_CALL", "call"), ("EL_PASSWORD", "pwd"),
                   ("EL_NAME", "name"), ("EL_QTH", "qth"),
                   ("EL_EMAIL", "email"), ("EL_NODE", "node")):
        v = cfg.get(k) or "INVALID"
        t = _set_key(t, key, v.upper() if key == "call" else v)
    t = _set_key(t, "astnode", node)
    _write(p, t)

    p = AST / "iax.conf"
    t = re.sub(r"^bindport\s*=\s*\d+", f"bindport = {cfg.get('IAX_PORT', '4569')}",
               p.read_text(), count=1, flags=re.M)
    _write(p, t)

    p = AST / "rpt_http_registrations.conf"
    t = p.read_text()
    t = re.sub(r"^;?\s*register\s*=>.*$", "", t, flags=re.M).rstrip() + "\n"
    if cfg.get("NODE") and cfg.get("NODE_PASSWORD"):
        t = t.replace("[registrations]", "[registrations]\nregister => "
                      f"{node}:{cfg['NODE_PASSWORD']}@register.allstarlink.org",
                      1)
    _write(p, t)

    _write(AST / "savenode.conf", f"NODE={node}\n")

    secret = manager_secret()
    p = AST / "manager.conf"
    t = re.sub(r"(\[admin\]\s*\nsecret\s*=\s*).*", rf"\g<1>{secret}",
               p.read_text(), count=1)
    _write(p, t)

    p = Path("/etc/allmon3/allmon3.ini")
    t = p.read_text().split("; --- k-link node ---")[0].rstrip() + "\n"
    t += (f"\n; --- k-link node ---\n[{node}]\nhost=127.0.0.1\n"
          f"user=admin\npass={secret}\n")
    _write(p, t)

    p = Path("/etc/allmon3/web.ini")
    title = cfg.get("DASH_TITLE") or f"k-link node {node}" + (f" {call}" if call else "")
    t = re.sub(r"^CONFIG_HEADER_TITLE\s*=.*$", f'CONFIG_HEADER_TITLE = "{title}"',
               p.read_text(), count=1, flags=re.M)
    _write(p, t)


def apply_entityid():
    """No ethernet on a Zero: give Asterisk its entity id from this board's
    wifi MAC (run at every boot, never at image build time)."""
    mac = next((Path(f"/sys/class/net/{i}/address").read_text().strip()
                for i in ("wlan0", "eth0", "end0", "usb0")
                if Path(f"/sys/class/net/{i}/address").exists()), "")
    if re.fullmatch(r"[0-9a-f:]{17}", mac) and mac != "00:00:00:00:00:00":
        p = AST / "asterisk.conf"
        t = p.read_text()
        t = re.sub(r"^;?\s*entityid\s*=.*\n", "", t, flags=re.M)
        t = t.replace("[options]", f"[options]\nentityid = {mac}", 1)
        _write(p, t)


# ------------------------------------------------------------------ others

def radio_user():
    """The login user (uid 1000): 'radio', or whatever Imager renamed it to."""
    import pwd
    return pwd.getpwuid(1000)


def klink_ini():
    if DOCKER:
        return Path(os.environ.get("KLINK_CONFIG", "/data/k-link.ini"))
    return Path(radio_user().pw_dir) / ".config/k-link/k-link.ini"


def apply_klink(cfg):
    ini = klink_ini()
    if not ini.exists():
        cmd = ["k-link", "-c", str(ini), "config"]
        if not DOCKER:
            cmd = ["runuser", "-u", radio_user().pw_name, "--"] + cmd
            ini.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["chown", f"{radio_user().pw_uid}:{radio_user().pw_gid}",
                            str(ini.parent)])
        subprocess.run(cmd, capture_output=True)
    if not ini.exists():
        return
    t = ini.read_text()
    subs = {"listen_port": cfg.get("USRP_KLINK_PORT", "34001"),
            "remote_host": "127.0.0.1",
            "remote_port": cfg.get("USRP_ASL_PORT", "32001"),
            "password": cfg["ADMIN_PASSWORD"]}
    if DOCKER:
        subs["port"] = cfg.get("KLINK_WEB_PORT", "8077")
    if cfg.get("BT_MAC"):
        subs["mac"] = cfg["BT_MAC"].upper()
    for key, val in subs.items():
        t = re.sub(rf"^{key}\s*=.*$", f"{key} = {val}", t, flags=re.M)
    _write(ini, t)


def apply_passwords(cfg):
    pw = cfg["ADMIN_PASSWORD"]
    if not DOCKER:
        subprocess.run(["chpasswd"], input=f"{radio_user().pw_name}:{pw}\n",
                       text=True, check=False)
    subprocess.run(["allmon3-passwd", "admin", "--password", pw],
                   capture_output=True)


def apply_wifi_country(cfg):
    if DOCKER:
        return
    c = cfg.get("WIFI_COUNTRY", "ES").upper()
    if re.fullmatch(r"[A-Z]{2}", c):
        subprocess.run(["raspi-config", "nonint", "do_wifi_country", c],
                       capture_output=True)


def add_wifi(ssid, password):
    """Store a wifi network in NetworkManager (connects when in range)."""
    name = f"wifi-{ssid}"
    subprocess.run(["nmcli", "con", "delete", name], capture_output=True)
    cmd = ["nmcli", "con", "add", "type", "wifi", "con-name", name,
           "ifname", "wlan0", "ssid", ssid,
           "connection.autoconnect", "yes",
           "connection.autoconnect-priority", "10"]
    if password:
        cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def klink_active():
    if DOCKER:
        out = subprocess.run(["supervisorctl", "status", "k-link"],
                             capture_output=True, text=True).stdout
        return "RUNNING" in out
    u = radio_user()
    return subprocess.run(["systemctl", "--user", "-M", f"{u.pw_name}@",
                           "is-active", "k-link"], capture_output=True,
                          text=True).stdout.strip() == "active"


def restart_services():
    if DOCKER:
        subprocess.run(["supervisorctl", "restart", "asterisk", "allmon3",
                        "k-link"], capture_output=True)
        return
    subprocess.run(["systemctl", "restart", "asterisk"], capture_output=True)
    subprocess.run(["systemctl", "restart", "allmon3"], capture_output=True)
    u = radio_user()
    subprocess.run(["systemctl", "--user", "-M", f"{u.pw_name}@",
                    "restart", "k-link"], capture_output=True)


def apply_all(cfg, restart=True):
    apply_wifi_country(cfg)
    apply_passwords(cfg)
    apply_asterisk(cfg)
    apply_klink(cfg)
    if restart:
        restart_services()
