# print-portal: OrcaSlicer slicing service (amd64 only)
FROM ubuntu:24.04 AS orca

ARG ORCA_URL=https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage
ARG ORCA_SHA256=d12fb8c8eac1aecd2dfb6377acd48f994f8fa439ed5292fa532dd82880f029fd

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fL --retry 3 --retry-delay 2 -o /tmp/OrcaSlicer.AppImage "$ORCA_URL"; \
    echo "$ORCA_SHA256  /tmp/OrcaSlicer.AppImage" | sha256sum -c -; \
    chmod +x /tmp/OrcaSlicer.AppImage; \
    cd /tmp && /tmp/OrcaSlicer.AppImage --appimage-extract >/dev/null; \
    mv /tmp/squashfs-root /opt/orca; \
    mv /opt/orca/bin/orca-slicer /opt/orca/bin/orca-slicer.real; \
    printf '%s\n' '#!/bin/sh' 'export LC_ALL=C' 'exec /opt/orca/libexec/orca-slicer-env /opt/orca/bin/orca-slicer.real "$@"' > /opt/orca/bin/orca-slicer; \
    chmod 0755 /opt/orca/bin/orca-slicer; \
    rm -f /tmp/OrcaSlicer.AppImage

FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8088 \
    DATA_DIR=/data \
    ORCA_BIN=/opt/orca/bin/orca-slicer \
    ORCA_DATADIR=/data/orca-datadir \
    HOME=/data/home \
    SEED_VERSION=2.4.2-2 \
    PATH=/opt/venv/bin:$PATH

RUN set -eu; [ "$(dpkg --print-architecture)" = "amd64" ] || { echo "print-portal images are amd64 only (the OrcaSlicer AppImage is x86_64)"; exit 1; }

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates fontconfig fonts-dejavu-core \
      libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 libcairo2 libcairo-gobject2 \
      libgdk-pixbuf-2.0-0 libgtk-3-0t64 libwebkit2gtk-4.1-0 libjavascriptcoregtk-4.1-0 \
      libsoup-3.0-0 libsecret-1-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
      libmspack0t64 libgl1 libglx0 libegl1 libopengl0 libglu1-mesa libx11-6 libxext6 \
      libsm6 libice6 libxkbcommon0 libwayland-client0 libwayland-egl1 libwayland-cursor0 \
      libwayland-server0 libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 \
      libharfbuzz0b libfontconfig1 libfreetype6 libdbus-1-3 libexpat1 liblzma5 zlib1g \
      libzstd1 libstdc++6 libevdev2 libgudev-1.0-0 libmanette-0.2-0 libenchant-2-2 \
      libglib2.0-0 libgio-2.0-0; \
    rm -rf /var/lib/apt/lists/*

COPY --from=orca /opt/orca /opt/orca

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

RUN set -eu; \
    if id ubuntu >/dev/null 2>&1; then userdel ubuntu || true; fi; \
    if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu || true; fi; \
    groupadd --gid 1000 app; \
    useradd --uid 1000 --gid 1000 --home-dir /data/home --shell /usr/sbin/nologin app; \
    mkdir -p /data/jobs /data/home && chown -R 1000:1000 /data

COPY app /app
COPY presets/datadir /opt/orca-datadir
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
COPY scripts/smoke_test.py /opt/smoke_test.py
COPY tests/fixtures/cube.stl /opt/smoke/cube.stl
COPY tests/fixtures/portal-import.orca_printer /opt/smoke/bundle.orca_printer

WORKDIR /app
USER app

RUN set -eu; mkdir -p /tmp/smoke; python3 /opt/smoke_test.py --orca /opt/orca/bin/orca-slicer --datadir /opt/orca-datadir --stl /opt/smoke/cube.stl --workdir /tmp/smoke --app-dir /app --bundle /opt/smoke/bundle.orca_printer; rm -rf /tmp/smoke

VOLUME ["/data"]
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 CMD python3 -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8088'), timeout=4).status == 200 else 1)"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]