# Setup

## Requirements

- Docker + docker-compose
- A media library that **Sonarr already manages** (Dubsmith reads it via the same path)
- **Sonarr v3** with API access
- **Crunchyroll Premium** subscription
- **Widevine CDM** — your own `device_client_id_blob.bin` + `device_private_key.pem`. Not bundled, not distributed.

## Installation

### 1. Pull the image and prepare directories

```bash
mkdir -p ~/dubsmith/data/widevine
cd ~/dubsmith
curl -O https://raw.githubusercontent.com/luisesk/dubsmith/main/docker-compose.yml
```

### 2. Edit `docker-compose.yml`

```yaml
services:
  dubsmith:
    image: ghcr.io/luisesk/dubsmith:latest
    user: "1000:1000"      # uid:gid that owns your media library
    volumes:
      - ./data:/data
      - /path/to/media:/library:rw
    ports:
      - "8080:8080"
    environment:
      - TZ=Etc/UTC          # set your timezone
```

Mount the media library at `/library`. The path Sonarr uses internally must match — Dubsmith remaps `sonarr_prefix` (default `/downloads`) to `/library` when it reads file paths from Sonarr.

If your Sonarr container uses `/tv` instead of `/downloads`, edit `data/config.yml`:

```yaml
paths_extra:
  sonarr_prefix: /tv
```

### 3. First-run config

```bash
docker compose run --rm dubsmith --help    # creates /data/config.yml from the example
```

Edit `data/config.yml` — at minimum:

```yaml
api:
  user: admin
  password: <pick a strong password>
```

### 4. Place your Widevine CDM

```bash
cp /path/to/device_client_id_blob.bin   data/widevine/
cp /path/to/device_private_key.pem      data/widevine/
chmod 600 data/widevine/*
```

### 5. Start

```bash
docker compose up -d
docker compose logs -f
```

Visit `http://localhost:8080`. Log in with the admin credentials from `data/config.yml`.

## Configure

### Sonarr

Settings → Sonarr → fill URL + API key → **Test connection**. Save.

### Crunchyroll

Settings → Sources → Crunchyroll → **Connect** → enter Premium credentials. The auth token is stored encrypted in `data/mdnx/install-config/cr_token.yml`.

### Add a show

Shows → pick a series from the Sonarr list → wizard searches Crunchyroll → pick the dub language and the per-season Crunchyroll IDs → save.

### Sonarr webhook (optional but recommended)

Sonarr → Settings → Connect → Add → Webhook:
- URL: `http://your-host:8080/sonarr-webhook`
- Triggers: `On Import`, `On Download`

New episodes auto-enqueue immediately instead of waiting for the next 6h scan.

### Reverse proxy (recommended for remote access)

Front Dubsmith with nginx / Caddy / Traefik / NPM with HTTPS. Cookie sessions use `SameSite=Lax`, so HTTPS is required to avoid leaking cookies on redirect.

## Backup

Back up `data/`:

```bash
tar -czf dubsmith-backup-$(date +%F).tar.gz \
    --exclude='data/staging' \
    -C ~/dubsmith data
```

`staging/` is transient. The rest (queue, users, settings, CDM, mdnx tokens) is what matters.
