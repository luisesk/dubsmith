# Troubleshooting

## Login

**Locked out / forgot password.**

Reset directly in the container:

```bash
docker exec dubsmith python3 -c \
  "from src.users import UsersStore; UsersStore('/data/users.yml').upsert('admin', password='NEWPASS', role='admin')"
```

## Sonarr

**`401 Unauthorized` or `Test connection` fails.**

- API key wrong (Sonarr → Settings → General → API Key).
- URL must be reachable *from inside the Dubsmith container* — `localhost`/`127.0.0.1` won't work; use the Docker network alias (`http://sonarr:8989`) or the host's LAN IP.

**`paths` mismatch.**

Dubsmith reads file paths from Sonarr's API but reads files from `/library`. Set `paths_extra.sonarr_prefix` in `data/config.yml` to whatever string Sonarr uses (e.g. `/tv`, `/downloads`, `/media`). The string is replaced with `paths.library_in_container`.

## Crunchyroll

**`Source returned 429`.**

CR rate-limited you. Lower `concurrency.downloads` to 1, restart container, wait 30 min.

**`No valid Widevine or PlayReady CDM detected`.**

Files missing or wrong. Check:

```bash
ls -la data/widevine/
# device_client_id_blob.bin
# device_private_key.pem
```

If the CDM file names differ (KeyDive sometimes outputs `client_id.bin` / `private_key.pem`), rename them.

**`license error` / `forbidden` for some episodes only.**

Geo-restriction. Some episodes are region-locked. Set `--proxy` in mdnx via `concurrency`/runtime args (not yet exposed in UI; edit `src/downloader.py` directly).

## Sync

**`low confidence (4.2); quarantining`.**

Cross-correlation couldn't find a clear peak. Causes:
- The downloaded source has different cut/length (Bluray cold-open vs WEBDL stream).
- Source is silent or pure music for the analyzed window.

Increase `sync.trim_seconds` to 180 s (analyzes more) or lower `sync.min_score` (accepts weaker matches — risky).

**Detected delay is correct first 5 minutes, off by a few seconds at the end.**

Different framerate (e.g. 23.976 vs 25 fps). Cross-correlation finds a single offset — drift across the episode means content has been re-timed (PAL speedup). Currently no fix; manual offset adjustment on a per-ep basis would need extra UI.

## Mux

**`mkvmerge` not found.**

Container should have it baked in. Rebuild image.

**Original file replaced but new file is smaller.**

Sanity check is `new_size > 90% * orig_size`. If your original has a much larger video bitrate than the dubs, this triggers a false alarm. Adjust threshold in `src/mux.py`.

## Performance

**Dashboard sluggish.**

The page polls `/queue` every 1.5 s and Sonarr-cached views may run heavy. Settings → Concurrency → reduce to 1 worker if your CPU is the bottleneck.

## Logs

**Where are the logs?**

- In-memory ring buffer (last 2000 lines) at `/logs` page in the UI.
- Full container stdout: `docker logs -f dubsmith`.

## Reset everything

```bash
docker compose down
rm -rf data/queue.db data/staging
docker compose up -d
```

This keeps users/shows/settings/CDM but wipes the job history.
