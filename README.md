# k-link

Radio gateway through a **Bluetooth K-plug PTT adapter**: link a handheld
radio to AllStarLink, DVSwitch, EchoLink (SvxLink), APRS and any other audio
application, with no cables or sound card between the computer and the radio.

```
                                             ┌─ USRP/UDP ── AllStarLink / DVSwitch
radio ──K plug── BT adapter ))) Bluetooth ((( k-link
                                             └─ virtual sound card ── SvxLink, Direwolf, ...
                                                (+ serial port PTT/COS /dev/ttyKLINK0)
```

Everything can be done from a **web page** (status, levels, pairing, test,
settings) or from the command line.

Wireless speaker-mic kits (sold under Abbree and other brands) come with two
pieces: the speaker-mic and a small adapter that plugs into the radio's
Kenwood-style K connector. k-link takes the place of the speaker-mic.

Status: working and tested end to end (USRP with AllStarLink and DVSwitch
Analog_Bridge, virtual sound card, serial PTT/COS with Direwolf) on Linux.
Windows, macOS and Android are not supported.

**Reference hardware.** k-link was developed and tested with the K-plug
adapter that comes with the **Abbree Bluetooth wireless speaker-mic** kit
(adapter + speaker-mic, [Abbree Wireless Bluetooth Speaker Microphone](https://abbree.com/products/abbree-wireless-bluetooth-speaker-microphone)). It shows up on Bluetooth as `YHD_BTA512_vKSTK07`;
its speaker-mic as `KST_vHMIC010`. Only the adapter is used: the
speaker-mic stays off. Test setup: TYT MD-380, Raspberry Pi 5, Raspberry Pi
OS (Debian 12). Other kits built around the same adapter should behave the
same; different models may need small changes.

## How it works

To Bluetooth, the adapter is a **phone** (HFP Audio Gateway + AVRCP) and
the speaker-mic is its headset. k-link is that headset:

| | adapter behaviour | k-link |
|---|---|---|
| PTT | AVRCP passthrough, acted on **release**: FF = key, REWIND = unkey | taps the keys through BlueZ |
| TX audio | opens an HFP call (SCO, mSBC 16 kHz) while keyed | plays the network audio into it |
| RX audio | opens a call **only while the radio outputs audio** | that call is the COS/squelch; its audio goes to the network |

So no VOX or audio-level detection is needed: the adapter does it.

## Requirements

- Linux with **BlueZ** and **PipeWire + WirePlumber** (default on current
  Raspberry Pi OS, Debian, Ubuntu). Python 3.9+, no Python packages.
- The adapter, and the radio on the channel/talkgroup to be gatewayed.
- On a headless machine WirePlumber must not wait for an active seat:

  ```
  # ~/.config/wireplumber/bluetooth.lua.d/51-headless.lua   (WirePlumber 0.4)
  bluez_monitor.properties["with-logind"] = false
  ```

## Install

Debian / Raspberry Pi OS / Ubuntu (pulls in BlueZ and PipeWire):

```sh
sudo apt install ./k-link_0.2.0_all.deb    # build it with packaging/build-deb.sh
```

or anywhere with Python 3.9+:

```sh
pipx install .       # from a clone of this repository
```

Then:

```sh
k-link config        # creates ~/.config/k-link/k-link.ini
```

### Pair the adapter (once)

```sh
k-link pair
```

Setting the Bluetooth device class needs root: k-link uses `sudo -n btmgmt`
when it can, otherwise allow it once or run `sudo btmgmt class 4 4` before.

Switch the speaker-mic **off**, then put the adapter in pairing mode (fast
blinking LED). The adapter does not advertise itself: it searches for its
speaker-mic, so k-link briefly poses as one (headset device class) until the
adapter connects. The YHD_BTA512 adapter pairs on the class alone; if another
model looks for its speaker-mic's name, add `--as-name <name>`. The MAC is saved to the config; afterwards k-link
reconnects to it by itself.

### Test

Put the radio on an echo/parrot channel (DMR: TG 9990) and run:

```sh
k-link test
```

```
TX 4.2 s ...
listening up to 20 s for the echo ...
RX 4.6 s, 4.7 s after unkey; peak -4.1 dBFS, rms -16.3 dBFS -> k-link-test.wav
PASS
```

It checks PTT, both audio paths and the RX detection, and warns when the
received level is too high or too low.

**Radio volume**: the K connector's audio output is strong, so the volume
knob usually belongs near its minimum (on the MD-380, 2–3 steps above it).
Adjust it until `k-link test` reports a peak of roughly -10 to -6 dBFS.

### Run as a service

```sh
systemctl --user enable --now k-link   # .deb install: unit already shipped
k-link install-service           # pipx install: writes the user unit and starts it
sudo loginctl enable-linger $USER   # start at boot without logging in
k-link status
```

## Web UI

`k-link run` (and the service) serves a page on port **8077**: live state
(idle / TX / RX), TX and RX level meters, activity log, buttons to pair the
adapter, run the loop test (and listen to its recording) and transmit test
beeps, and the settings below. It asks for the password written by
`k-link config` (any user name); with no password set it only listens on
localhost.

## Connecting it

### Virtual sound card

With `[soundcard] enabled = yes` two devices appear on the system:

- **k-link: to radio** (speaker): whatever an application plays here is
  transmitted. PTT by signal (VOX) with a short pre-roll so the first
  syllable is not lost, released after `vox_hang` seconds of silence.
- **k-link: from radio** (microphone): what the radio receives. The adapter
  only sends audio while the radio is receiving, so an application's own
  squelch/VOX detection works well.

Select them in SvxLink (EchoLink), Direwolf (APRS), Mumble, fldigi, ...

Applications that use ALSA directly (Direwolf, SvxLink `alsa:`) need
`pipewire-alsa`, and then find both directions as the ALSA device
**`klink`** (`ADEVICE klink` in Direwolf). The .deb installs it; otherwise
copy `packaging/alsa-k-link.conf` to `/etc/alsa/conf.d/60-k-link.conf`.
It does not change the system's default sound device.

Data modes (APRS, packet) only survive an analog FM channel: a digital
voice codec (DMR AMBE, ...) destroys the tones.

### Serial port PTT/COS

Instead of VOX, an application can key the radio through a virtual serial
port, like a classic radio interface: `/dev/ttyKLINK0`.

- **PTT**: the application raises **RTS** (or DTR, `serial_ptt_pin`); the
  radio transmits what is playing on "k-link: to radio" until it drops it.
- **COS / squelch**: **CTS** and **DCD** are on while the radio receives.
  **DSR** is always on ("device present").

The port is a real character device built with CUSE (part of the stock
Linux kernel), so it works with anything that does serial PTT: SvxLink
(`PTT_TYPE=SerialPin`, `SQL_DET=SERIAL`), Direwolf (`PTT /dev/ttyKLINK0
RTS`), fldigi, ... Allow it once, then choose "PTT by serial port" in the
web UI (or set `control = serial`):

```sh
k-link setup-serial      # asks for sudo
```

This creates group `klink` (allowed to create CUSE devices: only k-link
should need it), adds you to it and to `dialout`, and installs a udev rule
so the port belongs to group `dialout`. Very short PTT pulses (< 0.1 s,
e.g. when an application opens the port) are ignored, and the radio is
unkeyed when the application closes the port.

### USRP

k-link listens on `listen_port` for audio to transmit and sends what the
radio receives to `remote_host:remote_port`.

**AllStarLink** — a private node with a USRP channel, then link it to your
node (`*3<node>`):

```ini
; rpt.conf
[nodes]
1922 = radio@127.0.0.1/1922,NONE

[1922](node-main)
rxchannel = USRP/<k-link host>:34001:32002
```

```ini
; k-link.ini
[usrp]
listen_port = 34001
remote_host = <asterisk host>
remote_port = 32002
```

If your rpt.conf has a `[schedule]` running a link macro every minute (some
images do), give the k-link node an empty scheduler so it is not auto-linked:
`scheduler = sched-klink` plus an empty `[sched-klink]` stanza.

**DVSwitch Analog_Bridge** — point its `[USRP]` section at k-link the same way.

**Test peer** — `python3 -m klink.usrp_peer --send voice.wav --record back.wav`
plays the role of ASL: sends a WAV and records what comes back.

## Configuration

```ini
[bluetooth]
mac = 56:A0:12:F3:58:71

[usrp]
listen_host = 0.0.0.0
listen_port = 34001
remote_host = 127.0.0.1
remote_port = 32001
talkgroup = 0

[ptt]
tx_timeout = 180          ; hard limit for one transmission
net_hang = 0.5            ; unkey after this much network silence
rx_ignore_after_tx = 2.0  ; the adapter opens a ~1 s call after unkeying

[soundcard]
enabled = no
vox_threshold_db = -45    ; level that keys the radio
vox_hang = 1.0            ; silence that releases it
vox_preroll = 0.3         ; audio kept from before the VOX triggers
control = vox             ; vox, or serial: PTT/COS by /dev/ttyKLINK0
serial_device = ttyKLINK0
serial_ptt_pin = rts      ; rts or dtr
serial_cos_pins = cts,dcd ; lines raised while the radio receives

[web]
enabled = yes
port = 8077
password = <random>
```

Half duplex: while the radio is receiving, network audio is dropped.

## Troubleshooting

- **Broken / robotic RX audio**: Bluetooth range. Keep the adapter close to
  the computer; the Raspberry Pi shares one chip for Wi-Fi and Bluetooth.
  At ~6 % damaged mSBC frames speech breaks up; near the Pi it was 0.6 %.
- **Radio stuck in TX**: k-link always releases FF/REWIND with another key
  (BlueZ otherwise keeps repeating the press) and disconnects the adapter if
  it cannot unkey; disconnecting always unkeys the radio.
- **No audio nodes** (`k-link status` shows no call while keyed): check that
  PipeWire handles Bluetooth (`wpctl status` lists the adapter) and the
  headless WirePlumber setting above.
- **Why k-link rewires the adapter's audio**: in the Hands-Free role PipeWire
  presents the adapter like a phone call whose audio follows the default
  speaker and microphone (the radio would sound on the PC speakers and the PC
  microphone would transmit). k-link points those streams at an internal
  silent node (which also provides the audio clock) and cuts any other link.

## License

Apache-2.0
