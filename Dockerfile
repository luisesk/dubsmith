FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    MDNX_VERSION=v5.7.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg mkvtoolnix curl ca-certificates p7zip-full \
    && rm -rf /var/lib/apt/lists/*

# shaka-packager v2.6.1 (mdnx requires v2.x; v3.x not compatible)
RUN curl -fsSL "https://github.com/shaka-project/shaka-packager/releases/download/v2.6.1/packager-linux-x64" \
    -o /usr/local/bin/shaka-packager \
    && chmod +x /usr/local/bin/shaka-packager

# multi-downloader-nx (mdnx) — anime CR/Hidive downloader (pre-built CLI binary)
RUN mkdir -p /opt/mdnx \
    && curl -fsSL "https://github.com/anidl/multi-downloader-nx/releases/download/${MDNX_VERSION}/multi-downloader-nx-linux-x64-cli.7z" -o /tmp/mdnx.7z \
    && 7z x -y -o/opt/mdnx /tmp/mdnx.7z >/dev/null \
    && rm /tmp/mdnx.7z \
    && find /opt/mdnx -maxdepth 2 -type f -executable -name 'aniDL' -exec ln -sf {} /usr/local/bin/aniDL \; \
    && chmod -R a+rwX /opt/mdnx

WORKDIR /app
COPY pyproject.toml ./
# install only deps once (no src copy → no cache bust on app changes)
RUN pip install --no-cache-dir 'httpx>=0.27' 'numpy>=1.26' 'scipy>=1.13' 'PyYAML>=6.0' \
    'click>=8.1' 'fastapi>=0.115' 'uvicorn[standard]>=0.30' 'APScheduler>=3.10' \
    'Jinja2>=3.1' 'python-multipart>=0.0.9' 'itsdangerous>=2.2'

# Source goes in /app and is on PYTHONPATH at runtime — no site-packages copy
COPY src ./src
COPY web ./web
ENV PYTHONPATH=/app

VOLUME ["/data"]

# Make mdnx install dir + its widevine subdir world-writable so the container
# can run as any uid (the user picks via compose `user:`).
# Symlink mdnx widevine -> /data/widevine + mdnx config -> /data/mdnx/install-config
# so all mutable state lives under the single /data volume.
RUN rmdir /opt/mdnx/multi-downloader-nx-linux-x64-cli/widevine 2>/dev/null || true \
    && rm -rf /opt/mdnx/multi-downloader-nx-linux-x64-cli/widevine \
    && rm -rf /opt/mdnx/multi-downloader-nx-linux-x64-cli/config \
    && ln -s /data/widevine /opt/mdnx/multi-downloader-nx-linux-x64-cli/widevine \
    && ln -s /data/mdnx/install-config /opt/mdnx/multi-downloader-nx-linux-x64-cli/config

ENV DUBSMITH_CONFIG=/data/config.yml \
    DUBSMITH_DATA=/data \
    PLEX_DUB_CONFIG=/data/config.yml \
    PLEX_DUB_DATA=/data \
    HOME=/data/_home

ENTRYPOINT ["python3", "-m", "src.main"]
CMD ["daemon"]
