# k-link node: AllStarLink 3 + EchoLink + Allmon3 + k-link in one container.
# Multi-arch (linux/amd64, linux/arm64, linux/arm/v7). Build from the repo root:
#   docker buildx build -f docker/Dockerfile --platform linux/amd64,linux/arm64,linux/arm/v7 .
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG ASL_REPO=https://repo.allstarlink.org/public/asl-apt-repos.deb12_all.deb

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSLo /tmp/asl-repo.deb "$ASL_REPO" \
 && dpkg -i /tmp/asl-repo.deb \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      asl3 allmon3 apache2 \
      pipewire pipewire-bin wireplumber libspa-0.2-bluetooth \
      bluez dbus systemd supervisor sox fuse3 python3 procps iproute2 \
 && (apt-get purge -y 'linux-headers-*' || true) \
 && apt-get autoremove -y \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /tmp/*.deb

# k-link itself, from this repository
COPY src/klink /usr/lib/python3/dist-packages/klink
COPY packaging/alsa-k-link.conf /usr/share/alsa/alsa.conf.d/60-k-link.conf
RUN printf '#!/usr/bin/python3\nfrom klink.cli import main\nmain()\n' > /usr/bin/k-link \
 && chmod 755 /usr/bin/k-link \
 && find /usr/lib/python3/dist-packages/klink -name __pycache__ -prune -exec rm -rf {} +

# node panel (shared with the SD image) and container glue
COPY node/ /usr/lib/k-link-node/
COPY docker/rootfs/ /
RUN chmod 755 /usr/lib/k-link-node/web.py /usr/local/bin/k-link-node-entry \
 && a2enmod -q proxy proxy_http proxy_wstunnel rewrite headers \
 && a2dissite -q 000-default && a2ensite -q k-link-node

ENV KLN_MODE=docker \
    XDG_RUNTIME_DIR=/run/k-link \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/k-link/bus \
    KLINK_CONFIG=/data/k-link.ini \
    TZ=Etc/UTC

VOLUME /data
ENTRYPOINT ["/usr/local/bin/k-link-node-entry"]
