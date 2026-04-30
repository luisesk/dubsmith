# Architecture

## Components

```
┌─────────────────────────────────────────────────┐
│ Dubsmith daemon (Python)                        │
│                                                 │
│   FastAPI ─ web UI + REST + websocket           │
│   APScheduler ─ periodic library scan           │
│   N worker threads ─ download → sync → mux      │
│   SQLite queue ─ jobs.db                        │
│                                                 │
│   ─ src/sonarr.py     Sonarr v3 client          │
│   ─ src/downloader.py mdnx wrapper              │
│   ─ src/sync.py       FFT cross-correlation     │
│   ─ src/mux.py        mkvmerge wrapper          │
│   ─ src/users.py      PBKDF2 user store         │
│   ─ src/sources.py    streaming-source state    │
│   ─ src/settings_store.py   user settings       │
│   ─ src/security.py   throttle + validators     │
│   ─ src/logbuf.py     in-memory ring log        │
└─────────────────────────────────────────────────┘
        │                       │
        ▼                       ▼
   Sonarr API              Crunchyroll API
   (REST)                  (mdnx CLI subprocess)
```

## Pipeline (per episode)

1. **Scan** — Sonarr API lists episode files; ffprobe reports audio tracks per file. Episodes missing the target language and whose season is mapped to a Crunchyroll season ID are enqueued.
2. **Download** — `aniDL --service crunchy -s <CR_SEASON_ID> -e <N> --dubLang <code>` pulls the encrypted DASH segments, then shaka-packager decrypts using a user-provided Widevine CDM. Output is a transient `.mkv` in `/data/staging/<season>/<ep>/`.
3. **Sync** — extract first 120s of jpn audio from target file + downloaded source, FFT cross-correlation bounded to ±15s finds the offset (in ms). Confidence = peak / mean(abs(corr)).
4. **Mux** — `mkvmerge` writes a new `.mkv` containing the original video + audios + subs + the new audio track delayed by the detected offset (`--sync 0:Nms`). Tagged `<lang>:Portuguese Brazil` etc. Replaces the original file atomically.
5. **Sonarr** — optional rescan + unmonitor episode (prevents Sonarr re-grabbing a different release that overwrites the muxed file).

## Job state machine

```
   pending ─┐
            ▼
    downloading ─► syncing ─► muxing ─► done
            │         │          │
            ▼         ▼          ▼
         failed   quarantined  failed
            ▲
            │
        retry sweep (every 1h, up to max_attempts)
```

## Storage layout

```
/data/
├── config.yml              ← bootstrap defaults
├── settings.yml            ← UI-editable settings (Sonarr URL/key, sync thresholds, …)
├── users.yml               ← PBKDF2 user records
├── shows.yml               ← per-show CR-season mapping
├── sources.yml             ← Crunchyroll/Hidive/ADN connection state
├── queue.db                ← SQLite jobs table
├── widevine/               ← user's Widevine CDM (device_client_id_blob.bin + private_key.pem)
├── mdnx/
│   ├── install-config/     ← mdnx's cli-defaults.yml, dir-path.yml, cr_token.yml
│   └── config/             ← mdnx user config (auth tokens)
└── staging/                ← transient per-job download dirs
```

## Concurrency

- N worker threads (`settings.concurrency.downloads`, default 2). Each claims jobs atomically from SQLite.
- mdnx is invoked via `Popen` per job. Its `dir-path.yml` is shared, so the daemon serializes the **boot phase** of mdnx (write yml + spawn + 0.8 s grace) while the actual download/decrypt runs in parallel.
- Stale `downloading/syncing/muxing` jobs are reset to `pending` on daemon startup (recovery from crash mid-job).

## Sync algorithm details

- Target track: first jpn audio in the existing video (fallback to first audio if no jpn).
- Source: full audio track of the freshly downloaded `.mkv`.
- Both downsampled to 8 kHz mono.
- `scipy.signal.fftconvolve(target, source[::-1])` → cross-correlation.
- Search bounded to ±15 s around 0 lag.
- Score = peak / mean(abs(correlation)). Threshold default 10. Below → quarantine.
