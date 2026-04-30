# Dubsmith

> **Forge missing dubs into your library.** Auto-fetch language audio tracks from Crunchyroll and mux them into the video files you already have.

Dubsmith is a self-hosted companion for **Sonarr** + **Plex** / **Jellyfin** / **Emby**. It scans your library for episodes that lack a desired audio language (e.g. Portuguese dub on a Japanese-only Bluray rip), pulls the missing track from **Crunchyroll**, **Hidive**, or **AnimationDigitalNetwork (ADN)**, auto-syncs it against your existing video, and remuxes the file in-place — preserving your original quality.

![dashboard](docs/dashboard.png)

## Features

- 🔍 **Scans Sonarr** for episodes missing a target audio language
- 🌐 **Pulls dubs from Crunchyroll, Hidive, and AnimationDigitalNetwork (ADN)** via [multi-downloader-nx](https://github.com/anidl/multi-downloader-nx) — DRM-decrypted with your own Widevine CDM, per-show source selection
- 📐 **Auto-sync** via FFT cross-correlation against the original Japanese track
- 🎬 **Remuxes in place** with `mkvmerge` — no re-encode, original video bit-for-bit preserved
- 🔁 **Sonarr-aware** — optionally unmonitors episodes after mux so Sonarr won't re-grab a different release
- 📊 **Web UI** — dashboard, queue, library browser, per-show config, settings, logs, user management
- 🧵 **Multi-worker** — parallel downloads + sync + mux
- 🔐 **Auth** — multi-user with role-based access (admin / operator / viewer), PBKDF2 password hashing, login throttle

## Quick start

```bash
# 1. Get the compose file
mkdir dubsmith && cd dubsmith
curl -O https://raw.githubusercontent.com/luisesk/dubsmith/main/docker-compose.yml

# 2. Edit docker-compose.yml — set:
#    - user: "1000:1000"     # uid:gid that owns your media library
#    - /path/to/your/media:/library:rw
#    - TZ=Your/Timezone

# 3. Start
docker compose up -d
```

Then:

1. Visit `http://localhost:8080` → log in with the bootstrap admin (set in `data/config.yml` — default `admin` / `change-me-on-first-login`).
2. **Settings → Sonarr**: paste your Sonarr URL + API key, click **Test connection**.
3. **Settings → Sources → Crunchyroll**: click **Connect**, enter Premium credentials.
4. **Place Widevine CDM files** in `data/widevine/` (`device_client_id_blob.bin` + `device_private_key.pem`). You must supply your own — extracted from a device you own.
5. **Shows**: pick a series from the Sonarr list → wizard searches Crunchyroll, lets you pick the dub language, save mapping.
6. Sit back. Dubsmith scans every 6h (or on Sonarr import webhook); new dubs auto-mux.

## Documentation

- [docs/setup.md](docs/setup.md) — full installation + configuration guide
- [docs/architecture.md](docs/architecture.md) — how it works
- [docs/troubleshooting.md](docs/troubleshooting.md) — common issues

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

You need a **Crunchyroll Premium** subscription to download licensed content. Dubsmith is not affiliated with Crunchyroll. The Widevine CDM is **not** distributed; you extract your own from a device you own. Use of this tool may violate Crunchyroll's Terms of Service in some jurisdictions — make an informed decision.
