"""FastAPI app: dashboard, shows config, queue actions, library browser, webhook."""
import asyncio
import json
import logging
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, alerts, config, downloader, health, logbuf, probe, reconcile, scanner, security
from .audit import AuditLog
from .queue import Queue
from .settings_store import SettingsStore
from .shows import ShowsStore
from .sonarr import Sonarr
from .sources import SourcesStore
from .users import ROLES, UsersStore

WEBHOOK_MAX_BYTES = 256 * 1024  # 256 KB

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path("/app/web/templates")
STATIC_DIR = Path("/app/web/static")
if not TEMPLATES_DIR.exists():
    TEMPLATES_DIR = Path(__file__).parent.parent / "web" / "templates"
    STATIC_DIR = Path(__file__).parent.parent / "web" / "static"


def _sonarr_creds(cfg: dict, settings: SettingsStore | None = None) -> tuple[str, str]:
    """Settings override config.yml when set."""
    s = (settings.load().get("sonarr", {}) if settings else {}) or {}
    url = s.get("url") or cfg.get("sonarr", {}).get("url", "")
    api_key = s.get("api_key") or cfg.get("sonarr", {}).get("api_key", "")
    return url, api_key


def make_app(cfg: dict, queue: Queue, shows: ShowsStore,
             sources: SourcesStore | None = None,
             settings: SettingsStore | None = None,
             users: UsersStore | None = None) -> FastAPI:
    app = FastAPI(title="Dubsmith", version=__version__)
    login_throttle = security.LoginThrottle(max_attempts=5, window_seconds=300, lockout_seconds=900)
    pw_throttle = security.LoginThrottle(max_attempts=5, window_seconds=300, lockout_seconds=900)
    audit = AuditLog(config.data_dir() / "audit.log")
    # Session secret — persisted in settings.yml so cookies survive restarts.
    sec = (settings.load() if settings else {}).get("_session_secret") if settings else None
    if not sec and settings:
        sec = secrets.token_urlsafe(32)
        cur = settings.load()
        cur["_session_secret"] = sec
        settings.save(cur)
    # Cookie Secure flag: trust X-Forwarded-Proto from reverse proxy, else autodetect via env
    secure_cookie = os.environ.get("DUBSMITH_SECURE_COOKIE", "auto").lower()
    secure_flag = (secure_cookie == "true")
    app.add_middleware(SessionMiddleware,
                       secret_key=sec or secrets.token_urlsafe(32),
                       session_cookie="dubsmith_sess",
                       max_age=60 * 60 * 24 * 30, same_site="lax",
                       https_only=secure_flag)

    def _sonarr() -> Sonarr:
        url, key = _sonarr_creds(cfg, settings)
        return Sonarr(url, key)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["app_version"] = __version__

    # ---------- auth ----------
    fallback_user = (cfg.get("api") or {}).get("user", "admin")
    fallback_pass = (cfg.get("api") or {}).get("password", "")
    basic = HTTPBasic(auto_error=False)

    def _check(username: str, password: str) -> bool:
        if users and users.load():
            return users.verify(username, password)
        # legacy fallback
        return (secrets.compare_digest(username, fallback_user)
                and secrets.compare_digest(password, fallback_pass))

    def require_auth(request: Request, creds: HTTPBasicCredentials = Depends(basic)):
        # auth disabled?
        if not fallback_pass and (not users or not users.load()):
            return None
        # 1) session cookie
        sess_user = request.session.get("user")
        if sess_user:
            return sess_user
        # 2) HTTP Basic (for API/curl)
        if creds and _check(creds.username, creds.password):
            return creds.username
        # Browser? redirect to /login. API? 401.
        accept = request.headers.get("accept", "")
        if "text/html" in accept and request.method == "GET":
            raise HTTPException(
                status.HTTP_303_SEE_OTHER,
                headers={"Location": f"/login?next={request.url.path}"},
            )
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    def _user_role(username: str | None) -> str:
        if not username or not users:
            return "viewer"
        u = users.get(username) or {}
        return u.get("role", "viewer")

    def require_admin(username: str = Depends(require_auth)):
        if users and users.load() and _user_role(username) != "admin":
            raise HTTPException(403, "admin role required")
        return username

    def require_operator(username: str = Depends(require_auth)):
        """Operator can do queue + shows ops; viewer is read-only."""
        if users and users.load() and _user_role(username) not in ("admin", "operator"):
            raise HTTPException(403, "operator role required")
        return username

    # series_id -> name resolver (cached lazily; refreshed on each call from shows store)
    def resolve_name(sid: int) -> str:
        sh = shows.get(sid)
        if sh and sh.get("name"):
            return sh["name"]
        return f"Series {sid}"

    # ---------- login / logout (cookie session) ----------
    def _csrf_token(request: Request) -> str:
        """Per-session CSRF token, lazily generated and stored in session cookie."""
        tok = request.session.get("_csrf")
        if not tok:
            tok = secrets.token_urlsafe(32)
            request.session["_csrf"] = tok
        return tok

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str | None = None):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"next": next, "error": error, "csrf": _csrf_token(request)},
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request,
                           username: str = Form(...),
                           password: str = Form(...),
                           next: str = Form("/"),
                           csrf: str = Form("")):
        ip = request.client.host if request.client else "?"
        # CSRF: validate against session-bound token
        expected = request.session.get("_csrf", "")
        if not expected or not csrf or not secrets.compare_digest(csrf, expected):
            return templates.TemplateResponse(
                request=request, name="login.html",
                context={"next": next, "error": "session expired, retry",
                         "csrf": _csrf_token(request)},
                status_code=400,
            )
        # username sanity (prevents log poisoning + denial-of-bucket)
        if not security.valid_username(username):
            return templates.TemplateResponse(
                request=request, name="login.html",
                context={"next": next, "error": "invalid username format",
                         "csrf": _csrf_token(request)},
                status_code=400,
            )
        locked, secs = login_throttle.is_locked(ip, username)
        if locked:
            return templates.TemplateResponse(
                request=request, name="login.html",
                context={"next": next, "error": f"too many attempts; try again in {secs//60+1}min",
                         "csrf": _csrf_token(request)},
                status_code=429,
            )
        if _check(username, password):
            login_throttle.reset(ip, username)
            request.session["user"] = username
            # rotate csrf on auth state change
            request.session["_csrf"] = secrets.token_urlsafe(32)
            log.info("login ok: user=%s ip=%s", username, ip)
            audit.write(actor=username, ip=ip, action="login")
            return RedirectResponse(url=next or "/", status_code=303)
        login_throttle.record_failure(ip, username)
        log.warning("login fail: user=%s ip=%s", username, ip)
        audit.write(actor=username, ip=ip, action="login", ok=False)
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"next": next, "error": "invalid credentials",
                     "csrf": _csrf_token(request)},
            status_code=401,
        )

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    # ---------- json/api ----------
    @app.get("/health")
    def health():
        return {"ok": True, "stats": queue.stats(), "version": __version__}

    @app.get("/health/deep", dependencies=[Depends(require_auth)])
    def health_deep():
        """Reach out to Sonarr + (when configured) library server. Useful for monitoring."""
        import httpx
        out = {"version": __version__, "stats": queue.stats(), "ok": True, "checks": {}}
        url, key = _sonarr_creds(cfg, settings)
        if url and key:
            try:
                r = httpx.get(url.rstrip("/") + "/api/v3/system/status",
                              headers={"X-Api-Key": key}, timeout=5)
                out["checks"]["sonarr"] = {"ok": r.status_code == 200, "status": r.status_code}
                if r.status_code != 200:
                    out["ok"] = False
            except Exception as e:
                out["checks"]["sonarr"] = {"ok": False, "error": str(e)[:200]}
                out["ok"] = False
        else:
            out["checks"]["sonarr"] = {"ok": False, "error": "not configured"}
        s = (settings.load() if settings else {}).get("library_server") or {}
        if s.get("url") and s.get("token"):
            from .library_server import LibraryServer
            ls = LibraryServer(s.get("type", "plex"), s["url"], s["token"], s.get("section_id"))
            out["checks"]["library_server"] = ls.test()
            if not out["checks"]["library_server"].get("ok"):
                out["ok"] = False
        return out

    @app.get("/queue", dependencies=[Depends(require_auth)])
    def list_queue(state: str | None = None, limit: int = 200):
        return [_job_dict(j) for j in queue.list(state=state, limit=limit)]

    @app.get("/stats", dependencies=[Depends(require_auth)])
    def stats():
        return queue.stats()

    @app.post("/scan/{series_id}", dependencies=[Depends(require_operator)])
    def trigger_scan(series_id: int):
        show = shows.get(series_id) or cfg.get("shows", {}).get(series_id) or cfg.get("shows", {}).get(str(series_id))
        if not show:
            raise HTTPException(404, f"series {series_id} not configured")
        cr_seasons = show.get("cr_seasons") or {}
        sonarr = _sonarr()
        sonarr_prefix = (cfg.get("paths_extra") or {}).get("sonarr_prefix", "/downloads")
        target_lang = show.get("target_audio") or cfg["target_language"]["audio"]
        missing = scanner.find_missing(
            sonarr, series_id, target_lang,
            path_remap=(sonarr_prefix, cfg["paths"]["library_in_container"]),
        )
        n = 0
        skipped = 0
        for m in missing:
            if str(m.season) not in cr_seasons:
                skipped += 1
                continue  # season explicitly without CR dub mapping
            queue.upsert_pending(m.series_id, m.season, m.episode, m.target_path)
            n += 1
        return {"series_id": series_id, "enqueued": n, "skipped_no_cr_map": skipped}

    @app.post("/sonarr-webhook")
    async def sonarr_webhook(req: Request):
        # Shared-secret auth (configurable via settings.sonarr.webhook_secret).
        # Sonarr supports custom headers under "Webhook" connection settings.
        wh_secret = ((settings.load() if settings else {}).get("sonarr") or {}).get("webhook_secret", "")
        if wh_secret:
            sent = req.headers.get("x-webhook-secret") or req.query_params.get("secret") or ""
            if not secrets.compare_digest(sent, wh_secret):
                raise HTTPException(401, "invalid webhook secret")
        body = await req.body()
        if len(body) > WEBHOOK_MAX_BYTES:
            raise HTTPException(413, f"payload too large (>{WEBHOOK_MAX_BYTES} bytes)")
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")
        event = payload.get("eventType")
        series = payload.get("series") or {}
        sid = series.get("id")
        # Series-delete: drop from shows + clear pending queue.
        if event == "SeriesDelete":
            if not sid:
                return {"error": "no series.id"}
            removed = shows.delete(int(sid))
            n = reconcile._delete_series_pending(queue, int(sid))
            log.info("webhook SeriesDelete: sid=%d removed=%s cleared=%d", sid, removed, n)
            return {"event": "SeriesDelete", "series_id": sid,
                    "removed_show": removed, "cleared_jobs": n}
        if event not in ("Download", "Import"):
            return {"skipped": event}
        if not sid:
            return {"error": "no series.id"}
        show = shows.get(sid)
        if not (show and show.get("enabled", True)):
            return {"skipped": f"series {sid} disabled"}
        return trigger_scan(sid)

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
    def get_job(job_id: int):
        j = queue.get(job_id)
        if not j:
            raise HTTPException(404)
        return _job_dict(j)

    @app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_operator)])
    def retry_job(job_id: int):
        j = queue.get(job_id)
        if not j:
            raise HTTPException(404)
        queue.set_state(job_id, "pending", last_error=None)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/skip", dependencies=[Depends(require_operator)])
    def skip_job(job_id: int):
        j = queue.get(job_id)
        if not j:
            raise HTTPException(404)
        queue.set_state(job_id, "done", last_error="manually skipped")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/manual-delay", dependencies=[Depends(require_operator)])
    def manual_delay(job_id: int, payload: dict, request: Request,
                     current: str = Depends(require_auth)):
        """Operator override: set manual_delay_ms and re-enqueue. Worker will skip
        sync detection and mux using the supplied delay."""
        j = queue.get(job_id)
        if not j:
            raise HTTPException(404)
        try:
            delay = int(payload.get("delay_ms"))
        except (TypeError, ValueError):
            raise HTTPException(400, "delay_ms (int) required")
        if abs(delay) > 60_000:
            raise HTTPException(400, "delay must be within ±60000ms")
        queue.set_state(job_id, "pending", manual_delay_ms=delay,
                        last_error=f"manual delay {delay}ms")
        ip = request.client.host if request.client else "?"
        audit.write(actor=current, ip=ip, action="job.manual_delay",
                    target=str(job_id), delay_ms=delay)
        return {"ok": True, "delay_ms": delay}

    @app.post("/api/queue/retry-all", dependencies=[Depends(require_operator)])
    def retry_all_failed():
        n = queue.retry_failed(max_attempts=999)
        return {"requeued": n}

    @app.delete("/api/queue/clear", dependencies=[Depends(require_operator)])
    def clear_queue(state: str | None = None, error_like: str | None = None):
        n = queue.delete_where(state=state, error_like=error_like)
        return {"deleted": n}

    @app.post("/api/enqueue/series/{series_id}", dependencies=[Depends(require_operator)])
    def enqueue_series(series_id: int, season: int | None = None):
        """Enqueue all eps of a show (or just one season). Skips eps not in cr_seasons map and eps that already have target audio."""
        show = shows.get(series_id) or {}
        cr_seasons = show.get("cr_seasons") or {}
        sonarr = _sonarr()
        sonarr_prefix = (cfg.get("paths_extra") or {}).get("sonarr_prefix", "/downloads")
        target_lang = show.get("target_audio") or cfg["target_language"]["audio"]
        files = sonarr.episode_files(series_id)
        eps = {e["id"]: e for e in sonarr.episodes(series_id)}
        n = 0
        skipped = 0
        for f in files:
            ep = next((e for e in eps.values() if e.get("episodeFileId") == f["id"]), None)
            if not ep:
                continue
            s = ep["seasonNumber"]
            e = ep["episodeNumber"]
            if season is not None and s != season:
                continue
            if str(s) not in cr_seasons:
                skipped += 1
                continue
            langs = [l["name"] for l in (f.get("languages") or [])]
            target_name = {"por": "Portuguese", "eng": "English", "spa": "Spanish",
                           "fra": "French", "deu": "German", "ita": "Italian"}.get(target_lang, target_lang)
            if target_name in langs:
                skipped += 1
                continue
            target = f["path"].replace(sonarr_prefix, cfg["paths"]["library_in_container"], 1)
            jid = queue.upsert_pending(series_id, s, e, target)
            j = queue.get(jid)
            if j and j.state in ("done", "failed", "quarantined"):
                queue.set_state(jid, "pending", last_error=None)
            n += 1
        return {"enqueued": n, "skipped": skipped}

    @app.post("/api/enqueue", dependencies=[Depends(require_operator)])
    def enqueue_one(payload: dict):
        sid = int(payload["series_id"])
        season = int(payload["season"])
        episode = int(payload["episode"])
        target_path = payload.get("target_path")
        if not target_path:
            # Resolve from Sonarr
            sonarr = _sonarr()
            sonarr_prefix = (cfg.get("paths_extra") or {}).get("sonarr_prefix", "/downloads")
            for f in sonarr.episode_files(sid):
                eps = sonarr.episodes(sid)
                ep_match = next((e for e in eps if e.get("episodeFileId") == f["id"]
                                 and e["seasonNumber"] == season and e["episodeNumber"] == episode), None)
                if ep_match:
                    target_path = f["path"].replace(sonarr_prefix, cfg["paths"]["library_in_container"], 1)
                    break
        if not target_path:
            raise HTTPException(404, "ep not found in Sonarr")
        jid = queue.upsert_pending(sid, season, episode, target_path)
        # if existing job in terminal state, force back to pending
        j = queue.get(jid)
        if j and j.state in ("done", "failed", "quarantined"):
            queue.set_state(jid, "pending", last_error=None)
        return {"job_id": jid}

    @app.get("/api/metrics", dependencies=[Depends(require_auth)])
    def metrics():
        return {**queue.metrics(), "stats": queue.stats(),
                "per_series": queue.stats_per_series()}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom_metrics():
        """Prometheus text exposition. Unauthenticated by design — bind behind a
        reverse proxy ACL if exposing publicly."""
        s = queue.stats()
        m = queue.metrics()
        lines = [
            "# HELP dubsmith_info Build/version info",
            "# TYPE dubsmith_info gauge",
            f'dubsmith_info{{version="{__version__}"}} 1',
            "# HELP dubsmith_jobs Job count by state",
            "# TYPE dubsmith_jobs gauge",
        ]
        for state in ("pending", "downloading", "syncing", "muxing",
                      "done", "failed", "quarantined"):
            lines.append(f'dubsmith_jobs{{state="{state}"}} {s.get(state, 0)}')
        lines += [
            "# HELP dubsmith_done_total Jobs successfully completed",
            "# TYPE dubsmith_done_total counter",
            f'dubsmith_done_total {m.get("done_total", 0)}',
            "# HELP dubsmith_done_24h Jobs done in the last 24 hours",
            "# TYPE dubsmith_done_24h gauge",
            f'dubsmith_done_24h {m.get("done_24h", 0)}',
            "# HELP dubsmith_avg_abs_delay_ms Average absolute sync delay in ms",
            "# TYPE dubsmith_avg_abs_delay_ms gauge",
            f'dubsmith_avg_abs_delay_ms {m.get("avg_abs_delay_ms", 0)}',
            "# HELP dubsmith_avg_sync_score Average sync confidence score",
            "# TYPE dubsmith_avg_sync_score gauge",
            f'dubsmith_avg_sync_score {m.get("avg_sync_score", 0)}',
        ]
        return "\n".join(lines) + "\n"

    @app.websocket("/ws/queue")
    async def ws_queue(ws: WebSocket):
        # Auth: rely on session cookie (browser sends it automatically on ws upgrade).
        # If no users + no fallback password, auth is disabled — allow.
        auth_required = bool(fallback_pass or (users and users.load()))
        if auth_required:
            sess_user = ws.session.get("user") if hasattr(ws, "session") else None
            if not sess_user:
                # Cookie-less callers can pass ?user=&pass= for HTTP Basic equivalence
                u = ws.query_params.get("user", "")
                p = ws.query_params.get("pass", "")
                if not (u and p and _check(u, p)):
                    await ws.close(code=4401)
                    return
        await ws.accept()
        try:
            last = None
            while True:
                snap = {"stats": queue.stats(), "metrics": queue.metrics()}
                if snap != last:
                    await ws.send_text(json.dumps(snap))
                    last = snap
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            return

    # ---------- shows config ----------
    @app.get("/api/shows", dependencies=[Depends(require_auth)])
    def list_shows():
        return shows.load()

    @app.get("/api/shows/search", dependencies=[Depends(require_auth)])
    def search_shows(q: str, source: str = "crunchyroll"):
        try:
            return downloader.search_show(q, source=source)
        except Exception as e:
            raise HTTPException(500, f"mdnx search failed: {e}")

    @app.get("/api/cr/season/{cr_season_id}/dubs", dependencies=[Depends(require_auth)])
    def cr_season_dubs(cr_season_id: str, source: str = "crunchyroll"):
        return {"cr_season_id": cr_season_id,
                "source": source,
                "dubs": downloader.probe_season_dubs(cr_season_id, source=source)}

    @app.get("/api/shows/sonarr", dependencies=[Depends(require_auth)])
    def list_sonarr_series():
        sonarr = _sonarr()
        out = []
        for s in sonarr.all_series():
            out.append({
                "id": s["id"], "title": s["title"], "year": s.get("year"),
                "monitored": s.get("monitored"),
                "tvdbId": s.get("tvdbId"),
                "seasonCount": s.get("statistics", {}).get("seasonCount"),
                "episodeCount": s.get("statistics", {}).get("episodeCount"),
            })
        return out

    @app.post("/api/shows/quick-add", dependencies=[Depends(require_operator)])
    def quick_add(payload: dict):
        sid = int(payload["sonarr_id"])
        return shows.upsert(
            sid,
            name=payload.get("name", str(sid)),
            cr_seasons=payload.get("cr_seasons", {}),
            season_offset=payload.get("season_offset", {}),
            target_audio=payload.get("target_audio"),
            cr_dub_lang=payload.get("cr_dub_lang"),
            source=payload.get("source", "crunchyroll"),
            enabled=True,
        )

    @app.post("/api/shows/{series_id}", dependencies=[Depends(require_operator)])
    def upsert_show(series_id: int, payload: dict):
        return shows.upsert(series_id, **payload)

    @app.delete("/api/shows/{series_id}", dependencies=[Depends(require_operator)])
    def delete_show(series_id: int):
        ok = shows.delete(series_id)
        if not ok:
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/shows/{series_id}/toggle", dependencies=[Depends(require_operator)])
    def toggle_show(series_id: int):
        s = shows.get(series_id)
        if not s:
            raise HTTPException(404)
        shows.set_enabled(series_id, not s.get("enabled", True))
        return {"enabled": not s.get("enabled", True)}

    # ---------- HTML pages ----------
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def index(request: Request):
        jobs = queue.list(limit=50)
        names = {j.series_id: resolve_name(j.series_id) for j in jobs}
        return templates.TemplateResponse(
            request=request, name="dashboard.html",
            context={"stats": queue.stats(), "jobs": jobs, "names": names},
        )

    def _per_series_map() -> dict:
        return {row["series_id"]: row for row in queue.stats_per_series()}

    @app.get("/shows", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def shows_page(request: Request):
        sonarr = _sonarr()
        try:
            all_series = sonarr.all_series()
        except Exception:
            all_series = []
        tracked = shows.load()
        tracked_ids = set(int(k) for k in tracked.keys())
        # untracked anime-likely series sorted by title
        untracked = [
            {"id": s["id"], "title": s["title"], "year": s.get("year"),
             "monitored": s.get("monitored"),
             "episodeCount": s.get("statistics", {}).get("episodeCount", 0)}
            for s in all_series if s["id"] not in tracked_ids
        ]
        untracked.sort(key=lambda x: x["title"].lower())
        return templates.TemplateResponse(
            request=request, name="shows.html",
            context={"shows": tracked, "untracked": untracked,
                     "per_series": _per_series_map()},
        )

    @app.get("/shows/add/{series_id}", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def add_show_wizard(request: Request, series_id: int):
        sonarr = _sonarr()
        try:
            series = sonarr.series(series_id)
        except Exception as e:
            raise HTTPException(404, f"sonarr series {series_id}: {e}")
        # Run CR search automatically with title
        try:
            cr_results = downloader.search_show(series["title"], limit=8)
        except Exception as e:
            log.warning("CR search failed: %s", e)
            cr_results = []
        sonarr_seasons = sorted(
            [s for s in series.get("seasons", []) if s["seasonNumber"] > 0],
            key=lambda x: x["seasonNumber"],
        )
        existing = shows.get(series_id) or {}
        return templates.TemplateResponse(
            request=request, name="add_show.html",
            context={
                "series": series, "cr_results": cr_results,
                "sonarr_seasons": sonarr_seasons,
                "existing": existing,
            },
        )

    def _proxy_image(series_id: int, cover_type: str):
        import httpx
        sonarr = _sonarr()
        sonarr_url, sonarr_key = _sonarr_creds(cfg, settings)
        try:
            s = sonarr.series(series_id)
            for img in s.get("images", []) or []:
                if img.get("coverType") == cover_type:
                    url = img.get("remoteUrl") or img.get("url")
                    if url and url.startswith("/") and sonarr_url:
                        r = httpx.get(
                            sonarr_url.rstrip("/") + url,
                            headers={"X-Api-Key": sonarr_key},
                            timeout=10, follow_redirects=True,
                        )
                    elif url:
                        r = httpx.get(url, timeout=10, follow_redirects=True)
                    else:
                        continue
                    if r.status_code == 200:
                        from fastapi.responses import Response
                        return Response(content=r.content, media_type="image/jpeg",
                                        headers={"Cache-Control": "public, max-age=86400"})
        except Exception as e:
            log.warning("image fetch %s/%s: %s", series_id, cover_type, e)
        raise HTTPException(404)

    @app.get("/api/poster/{series_id}.jpg", dependencies=[Depends(require_auth)])
    def poster(series_id: int):
        return _proxy_image(series_id, "poster")

    @app.get("/api/fanart/{series_id}.jpg", dependencies=[Depends(require_auth)])
    def fanart(series_id: int):
        try:
            return _proxy_image(series_id, "fanart")
        except HTTPException:
            return _proxy_image(series_id, "banner")  # fallback to banner

    @app.get("/show/{series_id}", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def show_detail(request: Request, series_id: int):
        sonarr = _sonarr()
        show = shows.get(series_id) or {}
        try:
            series = sonarr.series(series_id)
        except Exception as e:
            raise HTTPException(404, f"sonarr {series_id}: {e}")
        try:
            files = sonarr.episode_files(series_id)
            eps = {e["id"]: e for e in sonarr.episodes(series_id)}
        except Exception:
            files = []; eps = {}
        rows = []
        for f in files:
            ep_match = next((e for e in eps.values() if e.get("episodeFileId") == f["id"]), None)
            season = ep_match["seasonNumber"] if ep_match else f.get("seasonNumber", 0)
            episode = ep_match["episodeNumber"] if ep_match else 0
            qjob = queue.by_series(series_id, season, episode)
            cr_mapped = str(season) in (show.get("cr_seasons") or {})
            qdict = None
            if qjob:
                qdict = {
                    "id": qjob.id, "state": qjob.state, "attempts": qjob.attempts,
                    "sync_delay_ms": qjob.sync_delay_ms, "sync_score": qjob.sync_score,
                    "last_error": qjob.last_error,
                }
            rows.append({
                "season": season, "episode": episode,
                "title": ep_match.get("title", "") if ep_match else "",
                "path": f.get("relativePath", ""),
                "quality": (f.get("quality") or {}).get("quality", {}).get("name", ""),
                "langs": [l["name"] for l in (f.get("languages") or [])],
                "queue": qdict,
                "cr_mapped": cr_mapped,
                "has_pt": "Portuguese" in [l["name"] for l in (f.get("languages") or [])],
            })
        rows.sort(key=lambda x: (x["season"], x["episode"]))
        return templates.TemplateResponse(
            request=request, name="show_detail.html",
            context={"sid": series_id, "series": series, "show": show, "rows": rows,
                     "page_title": series.get("title", "")},
        )

    # ---------- sources ----------
    @app.get("/api/sources", dependencies=[Depends(require_auth)])
    def list_sources():
        return sources.load() if sources else {}

    @app.post("/api/sources/{key}/connect", dependencies=[Depends(require_operator)])
    def connect_source(key: str, payload: dict):
        if not sources:
            raise HTTPException(503, "sources store not initialized")
        username = payload.get("username", "")
        password = payload.get("password", "")
        if not username or not password:
            raise HTTPException(400, "username + password required")
        # delegate to mdnx --auth
        src = sources.load().get(key)
        if not src:
            raise HTTPException(404, f"unknown source {key}")
        import subprocess
        try:
            r = subprocess.run(
                ["aniDL", "--service", src["service"], "--auth",
                 "--username", username, "--password", password],
                capture_output=True, text=True, timeout=60,
            )
            ok = "USER:" in r.stdout and "Anonymous" not in r.stdout.split("USER:")[-1].splitlines()[0]
            ok = ok or "successfully" in r.stdout.lower()
        except Exception as e:
            raise HTTPException(500, f"auth failed: {e}")
        if not ok:
            raise HTTPException(401, f"auth rejected: {(r.stdout + r.stderr)[-400:]}")
        sources.set_connected(key, username)
        return {"connected": True, "user": username}

    @app.post("/api/sources/{key}/disconnect", dependencies=[Depends(require_operator)])
    def disconnect_source(key: str):
        if not sources:
            raise HTTPException(503, "sources store not initialized")
        sources.disconnect(key)
        return {"connected": False}

    # ---------- users ----------
    @app.get("/api/users", dependencies=[Depends(require_admin)])
    def list_users():
        return users.list_safe() if users else []

    @app.post("/api/users", dependencies=[Depends(require_admin)])
    def create_user(payload: dict, request: Request, current: str = Depends(require_auth)):
        if not users:
            raise HTTPException(503)
        username = payload.get("username", "").strip()
        password = payload.get("password", "")
        role = payload.get("role", "operator")
        if not security.valid_username(username):
            raise HTTPException(400, "username must be 1-64 chars: letters, digits, . _ -")
        if len(password) < 8:
            raise HTTPException(400, "password must be ≥ 8 chars")
        if role not in ROLES:
            raise HTTPException(400, f"role must be one of {ROLES}")
        users.upsert(username, password=password, role=role)
        ip = request.client.host if request.client else "?"
        audit.write(actor=current, ip=ip, action="user.create",
                    target=username, role=role)
        return {"ok": True}

    @app.post("/api/users/{username}/password", dependencies=[Depends(require_auth)])
    def change_password(username: str, payload: dict, request: Request,
                        current: str = Depends(require_auth)):
        if not users:
            raise HTTPException(503)
        ip = request.client.host if request.client else "?"
        locked, secs = pw_throttle.is_locked(ip, current)
        if locked:
            raise HTTPException(429, f"too many attempts; try again in {secs//60+1}min")
        u = users.get(current)
        is_admin = u and u.get("role") == "admin"
        if not is_admin and current != username:
            pw_throttle.record_failure(ip, current)
            audit.write(actor=current, ip=ip, action="user.password_change",
                        target=username, ok=False, detail="forbidden")
            raise HTTPException(403, "can only change your own password")
        new_pass = payload.get("password", "")
        if len(new_pass) < 8:
            raise HTTPException(400, "password must be ≥ 8 chars")
        target = users.get(username)
        if not target:
            raise HTTPException(404)
        users.upsert(username, password=new_pass, role=target.get("role", "operator"))
        pw_throttle.reset(ip, current)
        audit.write(actor=current, ip=ip, action="user.password_change",
                    target=username, ok=True)
        return {"ok": True}

    @app.post("/api/users/{username}/role", dependencies=[Depends(require_admin)])
    def change_role(username: str, payload: dict, request: Request,
                    current: str = Depends(require_auth)):
        if not users:
            raise HTTPException(503)
        role = payload.get("role")
        if role not in ROLES:
            raise HTTPException(400, f"role must be one of {ROLES}")
        target = users.get(username)
        if not target:
            raise HTTPException(404)
        users.upsert(username, role=role)
        ip = request.client.host if request.client else "?"
        audit.write(actor=current, ip=ip, action="user.role_change",
                    target=username, role=role)
        return {"ok": True}

    @app.delete("/api/users/{username}", dependencies=[Depends(require_admin)])
    def delete_user(username: str, request: Request, current: str = Depends(require_auth)):
        if not users:
            raise HTTPException(503)
        if username == current:
            raise HTTPException(400, "cannot delete yourself")
        if not users.delete(username):
            raise HTTPException(404)
        ip = request.client.host if request.client else "?"
        audit.write(actor=current, ip=ip, action="user.delete", target=username)
        return {"ok": True}

    @app.get("/users", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    def users_page(request: Request, current: str = Depends(require_auth)):
        return templates.TemplateResponse(
            request=request, name="users.html",
            context={
                "users_list": users.list_safe() if users else [],
                "current_user": current,
                "roles": ROLES,
            },
        )

    # ---------- sonarr connection test ----------
    @app.post("/api/sonarr/test", dependencies=[Depends(require_operator)])
    def test_sonarr(payload: dict | None = None):
        url = (payload or {}).get("url") or _sonarr_creds(cfg, settings)[0]
        api_key = (payload or {}).get("api_key") or _sonarr_creds(cfg, settings)[1]
        if not url or not api_key:
            raise HTTPException(400, "url and api_key required")
        import httpx
        try:
            r = httpx.get(
                url.rstrip("/") + "/api/v3/system/status",
                headers={"X-Api-Key": api_key}, timeout=10,
            )
            if r.status_code == 200:
                d = r.json()
                return {"ok": True, "version": d.get("version"), "build": d.get("buildTime", "")[:10]}
            return {"ok": False, "status": r.status_code, "error": r.text[:200]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- settings ----------
    @app.get("/api/settings", dependencies=[Depends(require_auth)])
    def get_settings():
        return settings.load() if settings else {}

    @app.post("/api/settings/{section}", dependencies=[Depends(require_operator)])
    def update_settings(section: str, payload: dict, request: Request,
                        current: str = Depends(require_auth)):
        if not settings:
            raise HTTPException(503)
        out = settings.update(section, **payload)
        ip = request.client.host if request.client else "?"
        # avoid logging plaintext secrets
        scrubbed = {k: ("***" if k in ("api_key", "token", "password", "webhook_secret") else v)
                    for k, v in payload.items()}
        audit.write(actor=current, ip=ip, action="settings.update",
                    target=section, fields=scrubbed)
        return out

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def settings_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context={
                "sources": sources.load() if sources else {},
                "settings": settings.load() if settings else {},
            },
        )

    @app.get("/library", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def library_page(request: Request, sid: int | None = None):
        sonarr = _sonarr()
        tracked = shows.load()
        sections = []
        items = tracked.items() if sid is None else [(sid, tracked.get(sid) or tracked.get(str(sid)) or {})]
        for sid_raw, show in items:
            sid_int = int(sid_raw)
            cr_seasons = show.get("cr_seasons") or {}
            try:
                files = sonarr.episode_files(sid_int)
                eps = {e["id"]: e for e in sonarr.episodes(sid_int)}
            except Exception:
                files = []; eps = {}
            file_rows = []
            for f in files:
                fid = f["id"]
                ep_match = next((e for e in eps.values() if e.get("episodeFileId") == fid), None)
                season = ep_match["seasonNumber"] if ep_match else f.get("seasonNumber", 0)
                episode = ep_match["episodeNumber"] if ep_match else 0
                qjob = queue.by_series(sid_int, season, episode)
                langs = [l["name"] for l in (f.get("languages") or [])]
                file_rows.append({
                    "season": season, "episode": episode,
                    "path": f.get("relativePath", ""),
                    "quality": (f.get("quality") or {}).get("quality", {}).get("name", ""),
                    "langs": langs,
                    "queue_state": qjob.state if qjob else None,
                    "dubbable": str(season) in cr_seasons,
                })
            file_rows.sort(key=lambda x: (x["season"], x["episode"]))
            # Only count eps in *mapped* seasons toward missing-dub stats
            dubbable = [f for f in file_rows if f["dubbable"]]
            with_pt = sum(1 for f in dubbable if "Portuguese" in f["langs"])
            no_dub_total = len(file_rows) - len(dubbable)
            sections.append({
                "sid": sid_int, "name": show.get("name", str(sid_int)),
                "enabled": show.get("enabled", True),
                "files": file_rows,
                "total": len(file_rows),
                "dubbable_total": len(dubbable),
                "with_pt": with_pt,
                "no_dub_total": no_dub_total,
            })
        return templates.TemplateResponse(
            request=request, name="library.html", context={"sections": sections},
        )

    @app.get("/logs", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def logs_page(request: Request):
        return templates.TemplateResponse(request=request, name="logs.html", context={})

    @app.get("/api/logs.txt", response_class=PlainTextResponse, dependencies=[Depends(require_auth)])
    def logs_text(lines: int = 300, level: str | None = None):
        ring = logbuf.get()
        if not ring:
            return "(log buffer not initialized)"
        return "\n".join(ring.tail(n=lines, level=level)) or "(no entries)"

    # ---------- reconcile (Sonarr ↔ Dubsmith state sync) ----------
    @app.post("/api/shows/reconcile", dependencies=[Depends(require_operator)])
    def reconcile_shows(request: Request, current: str = Depends(require_auth)):
        result = reconcile.run(_sonarr(), shows, queue)
        ip = request.client.host if request and request.client else "?"
        audit.write(actor=current, ip=ip, action="shows.reconcile",
                    removed=len(result.get("removed") or []),
                    cleared=result.get("queue_cleared", 0))
        return result

    # ---------- discover (library-wide audio coverage scanner) ----------
    @app.get("/api/discover", dependencies=[Depends(require_auth)])
    def get_discover():
        from . import discover as _disc
        return _disc.load(config.data_dir())

    @app.post("/api/discover/scan", dependencies=[Depends(require_operator)])
    def trigger_discover_scan():
        from . import discover as _disc
        path_remap = (
            (cfg.get("paths_extra") or {}).get("sonarr_prefix", "/downloads"),
            cfg["paths"]["library_in_container"],
        )
        target_lang = cfg["target_language"]["audio"]
        tracked_raw = shows.load()
        tracked_ids = set(int(k) for k in tracked_raw.keys())
        tracked_cfg = {int(k): v for k, v in tracked_raw.items()}
        max_workers = int(cfg.get("scheduler", {}).get("discover_workers", 4))
        started = _disc.scan_in_background(
            _sonarr(), target_lang, path_remap, tracked_ids, config.data_dir(),
            tracked_cfg=tracked_cfg, max_workers=max_workers,
        )
        return {"started": started, "already_running": not started}

    @app.post("/api/discover/bulk-scan", dependencies=[Depends(require_operator)])
    def discover_bulk_scan(payload: dict, request: Request, current: str = Depends(require_auth)):
        """Trigger /scan for a list of tracked series_ids. Returns per-series counts."""
        ids = payload.get("series_ids") or []
        if not isinstance(ids, list):
            raise HTTPException(400, "series_ids must be a list")
        results: list[dict] = []
        sonarr = _sonarr()
        sonarr_prefix = (cfg.get("paths_extra") or {}).get("sonarr_prefix", "/downloads")
        default_lang = cfg["target_language"]["audio"]
        tracked = shows.load()
        for sid in ids:
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                results.append({"series_id": sid, "error": "invalid id"})
                continue
            sh = tracked.get(sid_int) or tracked.get(str(sid_int))
            if not sh:
                results.append({"series_id": sid_int, "error": "not tracked"})
                continue
            cr_seasons = sh.get("cr_seasons") or {}
            target_lang = sh.get("target_audio") or default_lang
            try:
                missing = scanner.find_missing(
                    sonarr, sid_int, target_lang,
                    path_remap=(sonarr_prefix, cfg["paths"]["library_in_container"]),
                )
            except Exception as e:
                results.append({"series_id": sid_int, "error": str(e)[:200]})
                continue
            n = 0; skipped = 0
            for m in missing:
                if str(m.season) not in cr_seasons:
                    skipped += 1
                    continue
                queue.upsert_pending(m.series_id, m.season, m.episode, m.target_path)
                n += 1
            results.append({"series_id": sid_int, "enqueued": n, "skipped": skipped})
        ip = request.client.host if request and request.client else "?"
        audit.write(actor=current, ip=ip, action="discover.bulk_scan",
                    count=len(ids), results=len(results))
        return {"results": results}

    @app.get("/discover", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def discover_page(request: Request):
        from . import discover as _disc
        d = _disc.load(config.data_dir())
        target_lang = cfg["target_language"]["audio"]
        return templates.TemplateResponse(
            request=request, name="discover.html",
            context={
                "data": d,
                "target_lang": target_lang,
                "summary": _disc.summary_counts(d.get("rows", [])),
            },
        )

    @app.get("/queue-page", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def queue_page(request: Request, state: str | None = None):
        jobs = queue.list(state=state, limit=200)
        names = {j.series_id: resolve_name(j.series_id) for j in jobs}
        return templates.TemplateResponse(
            request=request, name="queue.html",
            context={"jobs": jobs, "names": names,
                     "state": state, "stats": queue.stats()},
        )

    # ---------- self-service profile ----------
    AVATAR_DIR = config.data_dir() / "avatars"
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
    AVATAR_ALLOWED = {"image/png", "image/jpeg", "image/webp", "image/gif"}

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, current: str = Depends(require_auth)):
        u = (users.get(current) if users else None) or {}
        return templates.TemplateResponse(
            request=request, name="profile.html",
            context={"current_user": current, "role": u.get("role", "viewer")},
        )

    @app.get("/api/users/{username}/avatar")
    def get_avatar(username: str):
        # Sanitize: only allow valid usernames
        if not security.valid_username(username):
            raise HTTPException(404)
        for ext in (".png", ".jpg", ".webp", ".gif"):
            p = AVATAR_DIR / f"{username}{ext}"
            if p.exists():
                from fastapi.responses import FileResponse
                return FileResponse(p, headers={"Cache-Control": "private, max-age=300"})
        raise HTTPException(404)

    @app.post("/api/users/me/avatar")
    async def upload_avatar(request: Request, current: str = Depends(require_auth)):
        ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype not in AVATAR_ALLOWED:
            # Allow multipart upload too
            if not ctype.startswith("multipart/"):
                raise HTTPException(415, f"unsupported media type {ctype!r}")
        # Read body, bounded
        body = await request.body()
        if len(body) > AVATAR_MAX_BYTES:
            raise HTTPException(413, f"avatar too large (>{AVATAR_MAX_BYTES} bytes)")
        # Multipart parsing
        if ctype.startswith("multipart/"):
            from starlette.datastructures import UploadFile
            form = await request.form()
            f = form.get("file") or form.get("avatar")
            if not isinstance(f, UploadFile):
                raise HTTPException(400, "field 'file' required")
            ctype = (f.content_type or "").lower()
            if ctype not in AVATAR_ALLOWED:
                raise HTTPException(415, f"unsupported media type {ctype!r}")
            data = await f.read()
            if len(data) > AVATAR_MAX_BYTES:
                raise HTTPException(413, "avatar too large")
        else:
            data = body
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp", "image/gif": ".gif"}[ctype]
        # Remove any other-extension files for this user
        for old in AVATAR_DIR.glob(f"{current}.*"):
            try: old.unlink()
            except OSError: pass
        path = AVATAR_DIR / f"{current}{ext}"
        path.write_bytes(data)
        ip = request.client.host if request.client else "?"
        audit.write(actor=current, ip=ip, action="user.avatar_upload",
                    target=current, bytes=len(data))
        return {"ok": True, "size": len(data), "type": ctype}

    @app.delete("/api/users/me/avatar")
    def delete_avatar(request: Request, current: str = Depends(require_auth)):
        n = 0
        for old in AVATAR_DIR.glob(f"{current}.*"):
            try:
                old.unlink(); n += 1
            except OSError:
                pass
        ip = request.client.host if request.client else "?"
        audit.write(actor=current, ip=ip, action="user.avatar_delete", target=current)
        return {"ok": True, "removed": n}

    @app.post("/api/users/me/password")
    def change_my_password(payload: dict, request: Request,
                           current: str = Depends(require_auth)):
        if not users or not users.load():
            raise HTTPException(503, "user store not active")
        ip = request.client.host if request.client else "?"
        locked, secs = pw_throttle.is_locked(ip, current)
        if locked:
            raise HTTPException(429, f"too many attempts; try again in {secs//60+1}min")
        cur_pass = payload.get("current_password", "")
        new_pass = payload.get("password", "")
        if not users.verify(current, cur_pass):
            pw_throttle.record_failure(ip, current)
            audit.write(actor=current, ip=ip, action="user.password_change",
                        target=current, ok=False, detail="bad current password")
            raise HTTPException(401, "current password incorrect")
        if len(new_pass) < 8:
            raise HTTPException(400, "password must be ≥ 8 chars")
        target = users.get(current) or {}
        users.upsert(current, password=new_pass, role=target.get("role", "viewer"))
        pw_throttle.reset(ip, current)
        audit.write(actor=current, ip=ip, action="user.password_change",
                    target=current, ok=True)
        return {"ok": True}

    # ---------- audit ----------
    @app.get("/api/audit", dependencies=[Depends(require_admin)])
    def get_audit(limit: int = 200):
        return audit.tail(n=max(1, min(limit, 2000)))

    # ---------- admin: restart daemon ----------
    @app.post("/api/restart", dependencies=[Depends(require_admin)])
    def restart_daemon(request: Request, current: str = Depends(require_auth)):
        """SIGTERM ourselves; container restart-policy=unless-stopped will respawn.
        Only honored when run under docker compose with restart policy."""
        import os as _os
        import signal as _signal
        import threading as _threading
        ip = request.client.host if request and request.client else "?"
        audit.write(actor=current, ip=ip, action="daemon.restart")
        # Defer the SIGTERM by 200ms so this response can finish flushing
        def _kill():
            import time as _t
            _t.sleep(0.2)
            _os.kill(_os.getpid(), _signal.SIGTERM)
        _threading.Thread(target=_kill, daemon=True).start()
        return {"ok": True, "message": "restart requested; container should be back in <10s"}

    # ---------- alerts ----------
    @app.get("/api/alerts", dependencies=[Depends(require_auth)])
    def get_alerts():
        from . import alerts as _alerts
        return _alerts.list_alerts()

    @app.delete("/api/alerts/{key}", dependencies=[Depends(require_operator)])
    def clear_alert(key: str):
        from . import alerts as _alerts
        return {"cleared": _alerts.clear(key)}

    # ---------- staging maintenance ----------
    @app.get("/api/staging", dependencies=[Depends(require_auth)])
    def staging_status():
        from . import staging as _staging
        return _staging.staging_disk_usage(cfg["paths"]["staging"])

    @app.post("/api/staging/sweep", dependencies=[Depends(require_admin)])
    def staging_sweep(payload: dict | None = None, request: Request = None,
                      current: str = Depends(require_auth)):
        from . import staging as _staging
        max_age_days = float((payload or {}).get("max_age_days", 0))
        n = _staging.sweep_old(cfg["paths"]["staging"], max_age_days=max_age_days)
        ip = request.client.host if request and request.client else "?"
        audit.write(actor=current, ip=ip, action="staging.sweep",
                    max_age_days=max_age_days, removed=n)
        return {"removed_dirs": n, "max_age_days": max_age_days}

    # ---------- backup / restore ----------
    @app.get("/api/backup", dependencies=[Depends(require_admin)])
    def backup_data(request: Request, current: str = Depends(require_auth)):
        ip = request.client.host if request.client else "?"
        audit.write(actor=current, ip=ip, action="backup.download")
        """Stream a tar.gz of /data (excluding staging + caches). Admin only."""
        import io
        import tarfile
        from fastapi.responses import StreamingResponse
        data = config.data_dir()
        EXCLUDE = {"staging", "_home", "mdnx"}

        def _iter():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for p in sorted(data.iterdir()):
                    if p.name in EXCLUDE:
                        continue
                    try:
                        tf.add(p, arcname=p.name)
                    except Exception as e:
                        log.warning("backup skip %s: %s", p, e)
            buf.seek(0)
            while chunk := buf.read(64 * 1024):
                yield chunk

        import time as _t
        fname = f"dubsmith-backup-{int(_t.time())}.tar.gz"
        return StreamingResponse(
            _iter(),
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    return app


def _job_dict(j) -> dict:
    return {
        "id": j.id, "series_id": j.series_id, "season": j.season, "episode": j.episode,
        "state": j.state, "attempts": j.attempts, "sync_delay_ms": j.sync_delay_ms,
        "sync_score": j.sync_score, "last_error": j.last_error,
        "progress": getattr(j, "progress", 0) or 0,
        "bytes_done": getattr(j, "bytes_done", 0) or 0,
        "bytes_total": getattr(j, "bytes_total", 0) or 0,
        "phase": getattr(j, "phase", None),
        "target_path": j.target_path, "created_at": j.created_at,
        "updated_at": j.updated_at, "completed_at": j.completed_at,
    }
