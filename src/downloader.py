"""mdnx (multi-downloader-nx) wrapper for Crunchyroll audio-only downloads."""
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from . import security

log = logging.getLogger(__name__)

# Serializes the {write dir-path.yml + spawn aniDL} critical section.
# mdnx reads its config at boot, so once aniDL is running it has its own dir.
_MDNX_BOOT_LOCK = threading.Lock()


def probe_season_dubs(cr_season_id: str, timeout: int = 60) -> list[str]:
    """Return list of available dub language codes for a CR season ID via mdnx --series."""
    if not security.valid_cr_id(cr_season_id):
        return []
    try:
        r = subprocess.run(
            ["aniDL", "--service", "crunchy", "-s", cr_season_id],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:
        log.warning("probe_season_dubs failed: %s", e)
        return []
    out = r.stdout
    versions_re = re.compile(r"\s+- Versions:\s*(.+)$")
    langs: set[str] = set()
    for line in out.splitlines():
        m = versions_re.search(line)
        if m:
            for s in m.group(1).split(","):
                s = s.strip()
                if s:
                    langs.add(s)
    return sorted(langs)


def search_show(query: str, limit: int = 10) -> list[dict]:
    """Run aniDL search and parse top results into a list.

    Returns list of dicts: {show_id, title, seasons: [{cr_season_id, name, dub_langs}], audios, subs}
    """
    r = subprocess.run(
        ["aniDL", "--service", "crunchy", "-f", query],
        capture_output=True, text=True, timeout=120,
    )
    out = r.stdout
    # Parse blocks like:
    # [Z:GG5H5X3EE|SRZ.283393] Campfire Cooking ... (Seasons: 2, EPs: 24) [SUB, DUB]
    #     - Subtitles: ...
    #     [S:GR49C7EPD] Season 1 (Season: 1) [SUB, DUB]
    #       - Versions: en, es-419, pt-BR, de, ja
    show_re = re.compile(r"\[Z:([A-Z0-9]+)(?:\|[^\]]+)?\]\s+(.+?)\s+\(Seasons:")
    season_re = re.compile(r"\s+\[S:([A-Z0-9]+)\]\s+(.+?)\s+\(Season:")
    versions_re = re.compile(r"\s+- Versions:\s*(.+)$")

    shows: list[dict] = []
    current: dict | None = None
    for line in out.splitlines():
        m = show_re.search(line)
        if m:
            current = {"show_id": m.group(1), "title": m.group(2).strip(), "seasons": []}
            shows.append(current)
            continue
        if current is None:
            continue
        ms = season_re.search(line)
        if ms:
            current["seasons"].append({
                "cr_season_id": ms.group(1),
                "name": ms.group(2).strip(),
                "dub_langs": [],
            })
            continue
        mv = versions_re.search(line)
        if mv and current.get("seasons"):
            langs = [s.strip() for s in mv.group(1).split(",")]
            current["seasons"][-1]["dub_langs"] = langs

    # dedup by show_id (mdnx prints each row twice — once as series, once as season list parent)
    seen: dict[str, dict] = {}
    for s in shows:
        prev = seen.get(s["show_id"])
        # prefer the entry with more populated seasons
        if prev is None or len(s.get("seasons") or []) > len(prev.get("seasons") or []):
            seen[s["show_id"]] = s
    return list(seen.values())[:limit]


class MdnxDownloader:
    def __init__(self, staging_dir: str, widevine_dir: str,
                 dub_lang: str = "ptBR", sub_lang: str = "ptBR"):
        self.staging = Path(staging_dir)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.widevine = Path(widevine_dir)
        self.dub_lang = dub_lang
        self.sub_lang = sub_lang

    def _mdnx_config_dir(self) -> Path:
        # Persist user/profile config under /data so it survives container recreate
        # and works regardless of which uid the container runs as.
        return Path("/data/mdnx/config")

    def _ensure_widevine(self) -> None:
        target = self._mdnx_config_dir() / "widevine"
        target.mkdir(parents=True, exist_ok=True)
        for fname in ("device_client_id_blob.bin", "device_private_key.pem"):
            src = self.widevine / fname
            dst = target / fname
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())

    def _set_output_dir(self, content_dir: Path) -> None:
        """mdnx reads dir-path.yml from its install-dir config.
        We can't always write into the install dir (read-only fs / non-root), so
        symlink it to /data/mdnx/install-config on first call instead.
        """
        install_cfg = Path("/opt/mdnx/multi-downloader-nx-linux-x64-cli/config")
        install_cfg.mkdir(parents=True, exist_ok=True)
        (install_cfg / "dir-path.yml").write_text(
            f"content: {content_dir}/\nfonts: {content_dir}/.fonts/\n"
        )

    def download_audio(self, cr_season_id: str, ep_number: int, season: int,
                       on_progress=None) -> Path:
        """Run mdnx for one episode. Returns path to downloaded mkv.

        on_progress: callable(percent: float, phase: str, bytes_done: int|None, bytes_total: int|None)
                     called as mdnx emits parts/progress lines.
        """
        # Validate inputs (subprocess uses list args, but defense-in-depth)
        if not security.valid_cr_id(cr_season_id):
            raise ValueError(f"invalid CR season id: {cr_season_id!r}")
        if not isinstance(ep_number, int) or ep_number < 1 or ep_number > 9999:
            raise ValueError(f"invalid episode number: {ep_number!r}")
        if not security.valid_lang(self.dub_lang) or not security.valid_lang(self.sub_lang):
            raise ValueError(f"invalid lang code")
        self._ensure_widevine()
        out_dir = self.staging / cr_season_id / f"S{season:02d}" / f"E{ep_number:02d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        for stale in out_dir.glob("temp-*.m4s"):
            try:
                stale.unlink()
            except Exception:
                pass

        cmd = [
            "aniDL",
            "--service", "crunchy",
            "-s", cr_season_id,
            "-e", str(ep_number),
            "--dubLang", self.dub_lang,
            "--dlsubs", self.sub_lang,
            "--dlVideoOnce", "true",  # avoid rename collision when CR has lang-specific video tracks
            "--force", "y",
        ]
        log.info("mdnx: %s", " ".join(cmd))

        # mdnx output patterns
        parts_re = re.compile(r"(\d+) of (\d+) parts downloaded\s*\[(\d+)%\]\s*(?:\(([^)]+)\))?")
        mkv_re = re.compile(r"Progress:\s*(\d+)%")
        # phase markers — pattern, phase label, reset value
        # None = no progress reset, 0.0 = restart at 0, -1.0 = indeterminate (no bar)
        phase_markers = [
            ("Started decrypting audio", "decrypting audio", -1.0),
            ("Started decrypting video", "decrypting video", -1.0),
            ("Decryption done for audio", "decrypted audio", None),
            ("Decryption done for video", "decrypted video", None),
            ("Decryption Needed", "decrypting", -1.0),
            ("Requesting:", "requesting source", -1.0),
            ("Muxing", "muxing", 0.0),
            ("Skip muxing", "skip mux", None),
        ]
        # Track which "stream" is being downloaded (audio vs video) to label parts phase
        current_stream_phase = "downloading"

        # critical section: write dir-path.yml then spawn so this worker's
        # aniDL captures its own out_dir before another worker overwrites yml
        with _MDNX_BOOT_LOCK:
            self._set_output_dir(out_dir)
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            # give mdnx ~0.8s to read its config before releasing lock
            import time as _t
            _t.sleep(0.8)
        last_pct = -1.0
        last_n = 0
        last_lines: list[str] = []
        # mdnx uses \r for in-place updates; read in chunks and split by both \r and \n
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(4096) if proc.stdout else b""
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                buf += chunk
                # split on either \r or \n; keep trailing partial in buf
                while True:
                    idx = -1
                    for sep in (b"\r", b"\n"):
                        i = buf.find(sep)
                        if i >= 0 and (idx < 0 or i < idx):
                            idx = i
                    if idx < 0:
                        break
                    raw_line = buf[:idx]
                    buf = buf[idx+1:]
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    last_lines.append(line)
                    if len(last_lines) > 60:
                        last_lines = last_lines[-60:]
                    # parts download
                    m = parts_re.search(line)
                    if m:
                        done, total, pct = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        extra = m.group(4) or ""
                        if done < last_n - max(1, last_n * 0.05):
                            if "audio" in current_stream_phase:
                                current_stream_phase = "downloading video"
                            else:
                                current_stream_phase = "downloading"
                        last_n = done
                        label = f"{current_stream_phase} {done}/{total} parts" + (f" · {extra}" if extra else "")
                        if on_progress and pct != last_pct:
                            on_progress(pct / 100.0, label, None, None)
                            last_pct = pct
                        continue
                    m = mkv_re.search(line)
                    if m:
                        pct = int(m.group(1))
                        if on_progress and pct != last_pct:
                            on_progress(pct / 100.0, "mkvmerge muxing", None, None)
                            last_pct = pct
                        continue
                    for needle, phase, reset_val in phase_markers:
                        if needle in line:
                            if on_progress:
                                on_progress(reset_val, phase, None, None)
                                if reset_val is not None:
                                    last_pct = -2.0
                            if "audio" in phase:
                                current_stream_phase = "downloading audio"
                            elif "video" in phase:
                                current_stream_phase = "downloading video"
                            break
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()
        if proc.returncode != 0:
            tail = "\n".join(last_lines[-15:])
            log.error("mdnx exit %d. last lines:\n%s", proc.returncode, tail)
            # extract last meaningful error line
            err_line = next((l for l in reversed(last_lines)
                             if any(k in l.lower() for k in ("error", "fail", "blocked", "denied", "license"))),
                            last_lines[-1] if last_lines else "")
            raise RuntimeError(f"mdnx exit {proc.returncode}: {err_line[:200]}")

        outs = sorted(
            list(out_dir.glob("*.mkv")) + list(out_dir.glob("*.mp4")),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not outs:
            tail = "\n".join(last_lines[-15:])
            log.error("mdnx exited %d but produced no media. last lines:\n%s",
                      proc.returncode, tail)
            err_line = next((l for l in reversed(last_lines)
                             if any(k in l.lower() for k in
                                    ("error", "fail", "blocked", "denied", "license",
                                     "skip", "not found", "unavailable"))),
                            last_lines[-1] if last_lines else "no output")
            raise RuntimeError(f"no media — {err_line[:240]}")
        return outs[0]
