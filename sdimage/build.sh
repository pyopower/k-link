#!/bin/bash
# Build the k-link node image for Raspberry Pi Zero 2 W (also Pi 3/4/5).
# Must run on an arm64 Linux host (e.g. a Raspberry Pi) with sudo, from a
# clone of the k-link repository:
#   sdimage/build.sh           -> sdimage/dist/k-link-node-<date>.img.xz
set -euo pipefail
cd "$(dirname "$0")"
TOP=$PWD
REPO=$(cd .. && pwd)
BASE_URL=https://downloads.raspberrypi.com/raspios_oldstable_lite_arm64/images/raspios_oldstable_lite_arm64-2026-10-06/2026-10-06-raspios-bookworm-arm64-lite.img.xz
ASL_REPO=https://repo.allstarlink.org/public/asl-apt-repos.deb12_all.deb
IMG=$TOP/work.img
R=$TOP/root
SIZE=4G

cleanup() {
    set +e
    for d in dev/pts dev sys proc boot/firmware ""; do
        mountpoint -q "$R/$d" && sudo umount "$R/$d"
    done
    [ -n "${LOOP:-}" ] && sudo losetup -d "$LOOP"
}
trap cleanup EXIT

mkdir -p base dist
# k-link .deb from this checkout
"$REPO/packaging/build-deb.sh"
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/src/klink/__init__.py")
KLINK_DEB=$REPO/dist/k-link_${VER}_all.deb
B=base/$(basename $BASE_URL)
[ -f "$B" ] || { curl -fSLo "$B" $BASE_URL; curl -fsSLo "$B.sha256" $BASE_URL.sha256; }
(cd base && sha256sum -c "$(basename $B).sha256")

echo "== image"
xz -dc "$B" > "$IMG"
truncate -s $SIZE "$IMG"
sudo parted -s "$IMG" resizepart 2 100%
LOOP=$(sudo losetup -fP --show "$IMG")
sudo e2fsck -pf ${LOOP}p2
sudo resize2fs ${LOOP}p2
mkdir -p "$R"
sudo mount ${LOOP}p2 "$R"
sudo mount ${LOOP}p1 "$R/boot/firmware"
for d in proc sys dev dev/pts; do sudo mount --bind /$d "$R/$d"; done
sudo cp "$R/etc/resolv.conf" "$TOP/resolv.conf.orig" 2>/dev/null || true
sudo cp -L /etc/resolv.conf "$R/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' | sudo tee "$R/usr/sbin/policy-rc.d" >/dev/null
sudo chmod 755 "$R/usr/sbin/policy-rc.d"

echo "== packages"
curl -fsSLo "$TOP/asl-repo.deb" $ASL_REPO
sudo cp "$TOP/asl-repo.deb" "$KLINK_DEB" "$R/tmp/"
sudo chroot "$R" bash -euc "
export DEBIAN_FRONTEND=noninteractive
dpkg -i /tmp/asl-repo.deb
apt-get update -qq
apt-get install -y -qq asl3 allmon3 pipewire pipewire-bin wireplumber \
    libspa-0.2-bluetooth pipewire-alsa sox fuse3 acl python3
apt-get install -y -qq /tmp/$(basename "$KLINK_DEB")
# ASL3 drags in kernel headers; nothing is built here (no DKMS)
apt-get purge -y -qq 'linux-headers-*' || true
# save RAM on the Zero 2 W (512 MB): not needed on a headless node
apt-get purge -y -qq udisks2 modemmanager triggerhappy || true
apt-get autoremove -y -qq
apt-get clean
rm -f /tmp/*.deb
"

echo "== overlay"
sudo cp -r --no-preserve=ownership "$TOP/overlay/." "$R/"
sudo mkdir -p "$R/usr/lib/k-link-node"
sudo cp "$REPO/node/nodecfg.py" "$REPO/node/web.py" "$REPO/node/index.html" \
    "$R/usr/lib/k-link-node/"
sudo chmod 755 "$R/usr/lib/k-link-node/web.py"
sudo find "$R/usr/lib/k-link-node" "$R/usr/sbin" -name __pycache__ -prune -exec rm -rf {} +

echo "== system"
sudo chroot "$R" bash -euc '
# login user: radio / k-link (Raspberry Pi Imager can rename it and set a password)
if getent passwd pi >/dev/null; then
    usermod -l radio -d /home/radio -m pi
    groupmod -n radio pi
fi
echo "radio:k-link" | chpasswd
systemctl disable userconfig.service 2>/dev/null || true
systemctl mask userconfig.service
rm -f /etc/ssh/sshd_config.d/rename_user.conf
systemctl enable ssh
grep -q '^gpu_mem=' /boot/firmware/config.txt || echo gpu_mem=16 >> /boot/firmware/config.txt
echo k-link-node > /etc/hostname
sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tk-link-node/" /etc/hosts
# web: panel at / (proxy to 127.0.0.1:8078), Allmon3 at /allmon3/
a2enmod -q proxy proxy_http proxy_wstunnel rewrite headers
a2dissite -q 000-default
a2ensite -q k-link-node
systemctl enable k-link-node-boot k-link-node-wifi k-link-node-web asterisk allmon3 apache2
# first configuration with the defaults (private node 1999)
KLN_BUILD=1 /usr/sbin/k-link-node-boot
'
sudo rm -f "$R/usr/sbin/policy-rc.d"
if [ -f "$TOP/resolv.conf.orig" ]; then sudo cp "$TOP/resolv.conf.orig" "$R/etc/resolv.conf"; fi
sudo cp "$TOP/overlay/boot/firmware/k-link-node.txt" "$R/boot/firmware/"
sudo rm -rf "$R/var/lib/apt/lists/"* "$R/var/cache/apt/"*.bin
# fresh SSH host keys on first boot (Pi OS regenerates them if missing)
sudo rm -f "$R"/etc/ssh/ssh_host_*
# per-device secrets and identity, created at first boot
sudo rm -f "$R/etc/k-link-node/manager.secret"
sudo truncate -s 0 "$R/etc/machine-id"
sudo rm -f "$R/var/lib/dbus/machine-id"
sudo rm -rf "$R/root/.bash_history" "$R/home/radio/.bash_history" \
    "$R/var/log/apt/eipp.log.xz"

echo "== shrink"
sudo fstrim -v "$R" || true
cleanup; trap - EXIT; LOOP=
OUT=dist/k-link-node-$(date +%Y%m%d).img
mv "$IMG" "$OUT"
xz -T0 -6 -f "$OUT"
sha256sum "$OUT.xz" > "$OUT.xz.sha256"
ls -la dist
