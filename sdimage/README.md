# k-link node: SD card image for Raspberry Pi Zero 2 W

A ready-to-flash **AllStarLink / EchoLink node**. The radio (e.g. an MD-380
on analog FM) is linked with no cables through the Bluetooth adapter of a
wireless speaker-mic kit plugged into its K connector, using
[k-link](../README.md). Everything is managed from a web panel served by the
Pi itself (panel in Spanish; the k-link page is in English and Spanish).

Contents: Raspberry Pi OS Lite bookworm arm64 · AllStarLink 3 (Asterisk +
app_rpt + EchoLink) · Allmon3 · k-link · node panel · Wi-Fi setup hotspot.
It also boots on a Pi 3/4/5 (64-bit).

Download `k-link-node-<date>.img.xz` from the
[releases](https://github.com/pyopower/k-link/releases).

## Flash and start

1. Write the image with Raspberry Pi Imager ("Use custom") or
   `xzcat k-link-node-*.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync`.
   If Imager offers OS customisation you can set Wi-Fi, user and password
   there.
2. Optional: on your PC, edit **`k-link-node.txt`** in the `bootfs`
   partition: password, Wi-Fi, callsign, ASL node, EchoLink... It is applied
   on the first boot and then **deleted** (it holds passwords).
3. Boot the Pi. The first boot takes 2-3 minutes (it grows the filesystem
   and reboots).
4. Open **http://k-link-node.local/** (any user name, password `k-link`
   unless you changed it).
   With no known Wi-Fi, the Pi starts the network **K-LINK-SETUP** (password
   `k-link-setup`): join it, open **http://10.42.0.1/** and add your Wi-Fi.
5. In the k-link page (link "Radio (k-link)", port 8077) **pair the
   adapter**: put it in search mode (fast blinking) and press Pair. Then run
   the echo test to check the levels.

## Web pages

| Address | What for |
|---|---|
| `http://k-link-node.local/` | status, link/unlink, ASL/EchoLink settings, Wi-Fi, reboot |
| `http://k-link-node.local:8077/` | k-link: pairing, levels, echo test, PTT |
| `http://k-link-node.local/allmon3/` | Allmon3 (user `admin`) |

One admin password is used for everything: panel, k-link, Allmon3 and the
`radio` user over ssh.

## How it is put together

- ASL node (private **1999** unless you set a number) with
  `rxchannel = USRP/127.0.0.1:34001:32001` to k-link (listens on 34001,
  sends to 32001). `duplex = 1`: simplex radio, no repeat.
- EchoLink through ASL's `chan_echolink`: EchoLink nodes are linked as `3` +
  6 digits (e.g. `3123456`).
- Settings live in `/etc/k-link-node/node.conf`;
  `/usr/lib/k-link-node/nodecfg.py` (shared with the Docker image, from
  [`node/`](../node)) turns them into `rpt.conf`, `echolink.conf`,
  `modules.conf`, `rpt_http_registrations.conf`, `allmon3.ini` and
  `k-link.ini`.
- Services: `k-link-node-boot` (every boot), `k-link-node-wifi` (hotspot if
  there is no network after 60 s), `k-link-node-web` (panel on
  127.0.0.1:8078 behind Apache), `asterisk`, `allmon3`, and k-link as a user
  service of `radio`.
- Nothing device-specific is in the image: SSH host keys, machine id and the
  Asterisk manager secret are created on the first boot.

## Notes

- **Bluetooth and Wi-Fi share one chip and antenna** on the Zero 2 W: if
  the received audio breaks up, move the adapter closer or use a USB
  Bluetooth dongle.
- 512 MB RAM: the node uses about 330 MB. The image drops what is not
  needed (udisks2, ModemManager, pipewire-pulse) and sets `gpu_mem=16`.
- Licensing: check your country's rules. Operating it unattended usually
  makes it an automatic station, which may need a special licence. Set your
  callsign for the CW ID.
- Change the default `k-link` password: ssh is enabled.

## Build it yourself

On an arm64 Linux host (e.g. a Raspberry Pi) with sudo, from a clone of
this repository:

```sh
sdimage/build.sh          # -> sdimage/dist/k-link-node-<date>.img.xz
```
