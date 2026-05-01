# --- builder ----------------------------------------------------------------
# Pulls build-only tools (curl, p7zip, gcc for any wheel that needs it),
# downloads mdnx + shaka-packager, builds the python wheel cache. None of
# this junk needs to ship in the runtime image.
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    MDNX_VERSION=v5.7.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates p7zip-full \
    && rm -rf /var/lib/apt/lists/*

# shaka-packager v2.6.1 (mdnx requires v2.x; v3.x is incompatible)
RUN curl -fsSL "https://github.com/shaka-project/shaka-packager/releases/download/v2.6.1/packager-linux-x64" \
    -o /out/shaka-packager \
    --create-dirs \
    && chmod +x /out/shaka-packager

# multi-downloader-nx (mdnx) CLI binary
RUN mkdir -p /opt/mdnx \
    && curl -fsSL "https://github.com/anidl/multi-downloader-nx/releases/download/${MDNX_VERSION}/multi-downloader-nx-linux-x64-cli.7z" -o /tmp/mdnx.7z \
    && 7z x -y -o/opt/mdnx /tmp/mdnx.7z >/dev/null \
    && rm /tmp/mdnx.7z

# Pre-build all python wheels into /wheels — the runtime stage installs from
# this dir and never touches the network or pip's HTTP cache.
WORKDIR /build
COPY pyproject.toml ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels \
        'httpx>=0.27' 'numpy>=1.26' 'scipy>=1.13' 'PyYAML>=6.0' \
        'click>=8.1' 'fastapi>=0.115' 'uvicorn[standard]>=0.30' \
        'APScheduler>=3.10' 'Jinja2>=3.1' 'python-multipart>=0.0.9' \
        'itsdangerous>=2.2'


# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Runtime apt only — no curl, no p7zip, no compilers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg mkvtoolnix ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pull pre-built artifacts from the builder stage.
COPY --from=builder /out/shaka-packager /usr/local/bin/shaka-packager
COPY --from=builder /opt/mdnx /opt/mdnx
COPY --from=builder /wheels /wheels

# Install python deps from local wheels (no network, no pip cache layer).
RUN pip install --no-cache-dir --no-index --find-links /wheels \
        httpx numpy scipy PyYAML click fastapi 'uvicorn[standard]' \
        APScheduler Jinja2 python-multipart itsdangerous \
    && rm -rf /wheels

# Symlink mdnx CLI; relax permissions so any uid can use it.
RUN find /opt/mdnx -maxdepth 2 -type f -executable -name 'aniDL' \
        -exec ln -sf {} /usr/local/bin/aniDL \; \
    && chmod -R a+rwX /opt/mdnx

WORKDIR /app
COPY src ./src
COPY web ./web
ENV PYTHONPATH=/app

VOLUME ["/data"]

# Symlink mdnx widevine + config dirs into /data so they survive container
# recreate and work under any uid (chosen via compose `user:`).
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
