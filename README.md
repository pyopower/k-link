# k-link

Radio gateway through a **Bluetooth K-plug PTT adapter**: link a handheld
radio to AllStarLink, DVSwitch and anything else that speaks **USRP**, with
no cables or sound card between the computer and the radio.

```
radio ──K plug── BT adapter ))) Bluetooth ((( k-link ──USRP/UDP── ASL / DVSwitch / ...
```

Wireless speaker-mic kits (sold under Abbree and other brands) come with two
pieces: the speaker-mic and a small adapter that plugs into the radio's
Kenwood-style K connector. k-link takes the place of the speaker-mic.

Tested with the adapter `YHD_BTA512_vKSTK07` (pairs with the `KST_vHMIC010`
speaker-mic) on a TYT MD-380, Raspberry Pi 5, Raspberry Pi OS (Debian 12).

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

```sh
pipx install .       # from a clone of this repository
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
speaker-mic, so k-link briefly poses as one (name + headset class) until the
adapter connects. The MAC is saved to the config; afterwards k-link
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

### Run as a service

```sh
k-link install-service           # systemd user service, enabled and started
sudo loginctl enable-linger $USER   # start at boot without logging in
k-link status
```

## Connecting it

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
rx_ignore_after_tx = 1.5  ; the adapter opens a ~1 s call after unkeying
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

## License

Apache-2.0
