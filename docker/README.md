# k-link node in Docker

One container with **AllStarLink 3 + EchoLink + Allmon3 + k-link** and a small
web panel. The radio (e.g. an MD-380 on analog FM) is linked with no cables,
through the Bluetooth adapter of a wireless speaker-mic kit plugged into its
K connector. Everything is set with variables in `docker-compose.yml`.

Image: `ghcr.io/pyopower/k-link-node` — `linux/amd64` and `linux/arm64`
(Raspberry Pi 3/4/5/Zero 2 W with a **64-bit** OS). There is no working
32-bit ARM image: AllStarLink 3 does not publish Asterisk for armhf. On a
32-bit OS the container only prints that in its log.

## Quick start

```sh
mkdir k-link-node && cd k-link-node
curl -fsSLO https://raw.githubusercontent.com/pyopower/k-link/main/docker/docker-compose.yml
nano docker-compose.yml        # ADMIN_PASSWORD, CALLSIGN, ASLNODE, EchoLink...
docker compose up -d
docker logs k-link-node        # shows the addresses
```

1. Open the **k-link web** `http://<host>:8077/` and **pair** the adapter
   (put it in search mode: fast blinking). Then run the echo test. The
   paired MAC is kept in `./data/k-link.ini` (or set `BT_MAC`).
2. Panel: `http://<host>:8080/` (status, link/unlink nodes).
   Allmon3: `http://<host>:8080/allmon3/` (user `admin`).
   All of them use `ADMIN_PASSWORD`.
3. To change settings, edit `docker-compose.yml` and `docker compose up -d`
   again: the configuration is regenerated at every start.

## Variables

| Variable | Meaning |
|---|---|
| `ADMIN_PASSWORD` | panel, k-link web and Allmon3 password (default `k-link`: change it) |
| `CALLSIGN` | station callsign, sent in CW every 9 min |
| `ASLNODE`, `ASLPASS` | AllStarLink node and its password (empty = private node 1999) |
| `ECHOLINKCALLSIGN`, `ECHOLINKNODE`, `ECHOLINKPASS`, `ECHOLINKNAME`, `ECHOLINKQTH`, `ECHOLINKEMAIL` | EchoLink account (on when node and password are set) |
| `AC_ASL`, `AC_ECHOLINK` | nodes to connect at start, comma separated |
| `BT_MAC` | adapter MAC (optional; pairing from the k-link web stores it) |
| `WEB_PORT` | panel + Allmon3 (default 8080) |
| `KLINK_WEB_PORT` | k-link web (default 8077) |
| `IAX_PORT` | AllStarLink IAX2 udp port (default 4569) |
| `AMI_PORT` | Asterisk manager port, local only (default 5038; change it if the host runs its own Asterisk) |
| `DASH_TITLE` | Allmon3 title |
| `TZ` | time zone |

Open `IAX_PORT`/udp in the router for AllStarLink, and 5198-5199/udp +
5200/tcp for EchoLink.

## Host requirements

- **BlueZ running on the host**: `sudo systemctl enable --now bluetooth`.
  The container uses the host's Bluetooth through `/run/dbus`.
- `network_mode: host` and `privileged: true` are required: Bluetooth sockets
  (the SCO voice channel, the adapter class) only exist in the host network.
- **No other Bluetooth audio stack on the host.** On a desktop with PipeWire,
  disable its Bluetooth part, e.g. for WirePlumber 0.4:
  `~/.config/wireplumber/bluetooth.lua.d/90-off.lua` with
  `bluez_monitor.enabled = false`, then `systemctl --user restart wireplumber`.
  A headless server or Raspberry Pi OS Lite usually has none.
- Only one node with EchoLink per host (fixed EchoLink ports).

## Start checks

At every start the container checks the requirements above. Every problem
is written to the log (`docker logs k-link-node`) and shown as a red banner
in the panel:

| Check | Message |
|---|---|
| `/run/dbus` mounted | `no /run/dbus/system_bus_socket` (the only one that stops the start) |
| host network | `no Bluetooth sockets: set network_mode: host` |
| privileged | `not privileged: set privileged: true` |
| BlueZ on the host, adapter present and powered | `BlueZ is not running`, `no Bluetooth adapter`, `adapter ... is off` |
| no other Bluetooth audio stack on the host | `another Bluetooth audio stack ... owns the hands-free profile` |
| free ports (`WEB_PORT`, `KLINK_WEB_PORT`, `IAX_PORT`, `AMI_PORT`, USRP, Allmon3) | `port N/tcp|udp (...) is already in use` |
| one EchoLink node per host | `EchoLink ports ... are in use` |
| 32-bit ARM | `32-bit ARM is not supported` |

The Bluetooth audio check is only made at start: if PipeWire is started on
the host later, restart the container to check again.

## Build it yourself

```sh
docker buildx build -f docker/Dockerfile \
  --platform linux/amd64,linux/arm64 -t k-link-node .
```

The GitHub workflow `.github/workflows/docker.yml` publishes it to
`ghcr.io/<owner>/k-link-node` on every push to `main` and on `v*` tags.
