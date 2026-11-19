"""ClipForge — local web app: YouTube link / upload -> 5 polished captioned vertical clips.

Run:  python server.py   then open http://localhost:8765
Standard library only. One worker thread renders jobs one at a time (low-RAM machine).
"""
import json, mimetypes, queue, re, shutil, subprocess, threading, time, traceback, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

import config as C
import captions
import pipeline as P
import render as R
import facecam
import effects as FX
import highlights as HL
import live as LV

PORT = int(__import__("os").environ.get("PORT", 8765))
LOCK = threading.RLock()
LIVE = {}                       # job id -> live.LiveSession
WORK = queue.Queue()
JOBS = {}


# ------------------------------------------------------------------ job store
def slug(s, n=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s or "").strip("-").lower()
    return s[:n].strip("-") or "clip"


def save(job):
    with LOCK:
        d = C.JOBS / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=1), encoding="utf-8")
        tmp.replace(d / "job.json")


def load_all():
    for f in C.JOBS.glob("*/job.json"):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            if j.get("type") == "live" and j["status"] == "live":   # session can't survive a restart
                j["status"] = "done"
                j.setdefault("live", {})["state"] = "ended"
            if j["status"] in ("queued", "working"):     # interrupted by a restart
                j["status"], j["error"] = "error", "Server restarted while this was running — press Retry."
            for o in j.get("outputs", {}).values():
                if o.get("status") in ("queued", "rendering"):
                    o["status"] = "error"
            JOBS[j["id"]] = j
        except Exception:
            pass


def log(job, msg):
    with LOCK:
        job["log"].append({"t": time.strftime("%H:%M:%S"), "m": msg})
        job["stage"] = msg
        save(job)


def source_dir(job):
    s = job["source"]
    if s["type"] == "url":
        vid = P.video_id(s["url"])
        return C.ROOT / "raw" / vid
    return C.UPLOADS / s["upload_id"]


def find_source(d: Path):
    for p in sorted(d.glob("source.*")):
        if p.suffix.lower() in C.VIDEO_EXT:
            return p
    return None


# ------------------------------------------------------------------ worker
def do_analyze(job):
    d = source_dir(job)
    s = job["source"]
    if s["type"] == "url":
        src = P.fetch_youtube(s["url"], d, lambda m: log(job, m), stream=is_stream(job))
        t = d / "title.txt"
        if t.exists():
            job["title"] = t.read_text(encoding="utf-8").strip()
        elif (d / "meta.json").exists():
            try:
                job["title"] = json.loads((d / "meta.json").read_text(encoding="utf-8")).get("title", "")
            except Exception:
                pass
    else:
        src = find_source(d)
        if not src:
            raise P.PipelineError("Uploaded file is missing.")
    info = P.probe(src)
    if not info["width"]:
        raise P.PipelineError("That file has no video stream.")
    job["info"] = info
    log(job, f"Source: {info['width']}×{info['height']}, {info['duration'] / 60:.1f} min")
    if job["settings"].get("layout") in ("stream", "auto"):
        run_detect(job, src)
        if job["settings"].get("layout") == "auto":   # facecam -> Streamer cam, else whole frame on blur
            found = (job.get("facecam") or {}).get("found")
            job["settings"]["layout"] = "stream" if found else "fit"
            log(job, "Layout: Streamer cam (facecam found)." if found else
                "Layout: Fit + blur (no facecam found — keeps the whole gameplay frame).")
    words = P.transcribe(src, d, lambda m: log(job, m))
    if len(words) < 20:
        raise P.PipelineError("Hardly any speech found — this video needs someone talking.")
    st = job["settings"]
    cands = None
    if is_stream(job):
        loud = P.ensure_loudness(src, d, lambda m: log(job, m))
        log(job, "Scanning the stream for hype: audio spikes, chat bursts, reactions…")
        cands, parts = HL.candidates(words, P.sentences(words), loud, d, info["duration"],
                                     int(st["count"]), float(st["min_len"]), float(st["max_len"]))
        chat = parts.get("chat_total", 0)
        log(job, f"Found {len(cands)} hype peaks" + (f" (using {chat:,} chat messages)." if chat else
                                                       " (no chat replay — audio and reactions only)."))
    clips = P.pick_moments(words, int(st["count"]), float(st["min_len"]), float(st["max_len"]),
                           bool(st.get("ai_pick", True)), lambda m: log(job, m), job.get("title", ""), cands=cands)
    job["clips"] = clips
    job["status"] = "ready"
    log(job, f"Found {len(clips)} moments — review them and hit Render.")


def is_stream(job):
    s = job["source"]
    return job["settings"].get("mode") == "stream" or (s["type"] == "url" and P.platform(s["url"]) in ("twitch", "kick"))


def run_detect(job, src=None):
    src = src or find_source(source_dir(job))
    log(job, "Looking for the streamer's facecam…")
    r = facecam.detect(src, job["info"]["duration"], C.JOBS / job["id"], lambda m: log(job, m))
    with LOCK:
        job["settings"]["face_box"] = r["face_box"]
        job["settings"]["game_box"] = r["game_box"]
        job["facecam"] = {"found": r["found"], "frame": "/media/data/" + r["frame"].relative_to(C.DATA).as_posix(),
                          "v": int(time.time())}
        save(job)


def do_render(job):
    d = source_dir(job)
    src = find_source(d)
    words = P.load_words(d / "words.json")
    out_dir = C.EXPORT / f"{slug(job.get('title') or 'clips', 30)}-{job['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    job["export_dir"] = str(out_dir)
    jd = C.JOBS / job["id"]
    sel = [c for c in job["clips"] if c.get("selected")]
    for k, c in enumerate(sel):
        o = job["outputs"].setdefault(c["id"], {})
        if o.get("status") == "done" and o.get("sig") == sig(job, c):
            continue
        o.update(status="rendering")
        log(job, f"Rendering clip {k + 1}/{len(sel)}: {c['title']}")
        name = f"{k + 1:02d}-{slug(c['title'])}.mp4"
        out = out_dir / name
        try:
            dur = R.render_clip(jd, src, job["info"], words, c, job["settings"], k, out)
            o.update(status="done", file=str(out), dur=dur, sig=sig(job, c),
                     url="/media/export/" + out.relative_to(C.EXPORT).as_posix(),
                     thumb="/media/export/" + out.with_suffix(".jpg").relative_to(C.EXPORT).as_posix(),
                     v=int(time.time()))
        except Exception as e:
            o.update(status="error", error=str(e)[-400:])
            log(job, f"Clip {k + 1} failed: {str(e)[:200]}")
        save(job)
    write_meta(job)
    job["status"] = "done"
    log(job, f"Done — clips saved to {out_dir}")


def write_meta(job, force=False):
    """Title / description / hashtags for every rendered clip that doesn't have fresh ones."""
    need = [c for c in job["clips"]
            if job["outputs"].get(c["id"], {}).get("status") == "done"
            and (force or job["outputs"][c["id"]].get("meta_sig") != job["outputs"][c["id"]].get("sig"))]
    if not need:
        return
    url = job["source"].get("url", "") if job["source"]["type"] == "url" else ""
    meta = P.make_meta(need, job.get("title", ""), url, bool(job["settings"].get("ai_pick", True)),
                       lambda m: log(job, m))
    for c in need:
        o = job["outputs"][c["id"]]
        o["meta"] = meta[c["id"]]
        o["meta_sig"] = o.get("sig")
    save(job)
    log(job, f"Titles & descriptions ready for {len(need)} clip(s).")


def sig(job, c):
    return json.dumps([c["start"], c["end"], c["title"], c.get("hook_text"), c.get("tease"), c.get("keywords"), c.get("emojis"),
                       job["settings"]], sort_keys=True)


def worker():
    while True:
        jid, action = WORK.get()
        job = JOBS.get(jid)
        if not job:
            continue
        try:
            job["status"], job["error"] = "working", None
            save(job)
            if action == "meta":
                write_meta(job, force=True)
                job["status"] = "done"
            else:
                (do_analyze if action == "analyze" else do_render)(job)
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
            traceback.print_exc()
        save(job)


# ------------------------------------------------------------------ helpers
DEFAULT_SETTINGS = dict(count=5, min_len=30, max_len=40, ai_pick=True, layout="split", gameplay="mix",
                        avoid_burned=True, position="auto", preset="pop", style=dict(captions.PRESETS["pop"]), seed=7,
                        polish=dict(FX.DEFAULT_POLISH))


def gameplay_list():
    out = []
    for p in R.gameplay_files():
        try:
            i = P.probe(p)
        except Exception:
            i = {"duration": 0, "width": 0, "height": 0}
        out.append({"name": p.name, "duration": round(i["duration"]), "width": i["width"], "height": i["height"],
                    "size_mb": round(p.stat().st_size / 1e6, 1), "thumb": f"/api/gameplay-thumb/{p.name}"})
    return out


def gameplay_thumb(name):
    src = C.GAMEPLAY / name
    t = C.THUMBS / (slug(name) + ".jpg")
    if not t.exists() or t.stat().st_mtime < src.stat().st_mtime:
        d = P.probe(src)["duration"]
        subprocess.run([C.FFMPEG, "-v", "error", "-y", "-ss", f"{min(60, d / 3):.1f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=480:270:force_original_aspect_ratio=increase,crop=480:270", "-q:v", "4", str(t)],
                       capture_output=True)
    return t


def style_preview(settings):
    """Real libass render of the chosen caption style over a gameplay frame (no job needed)."""
    st = {**captions.PRESETS.get(settings.get("preset", "pop"), captions.PRESETS["pop"]), **settings.get("style", {})}
    layout = settings.get("layout", "split")
    pos = R.default_pos(layout, settings.get("position", "auto"))
    key = uuid.uuid4().hex[:8]
    ass = C.THUMBS / f"prev_{key}.ass"
    out = C.THUMBS / f"prev_{key}.jpg"
    captions.build(captions.SAMPLE, st, 1.45, pos, ass)
    files = R.gameplay_files()
    rel = ass.relative_to(C.APP).as_posix()
    if files:
        g = files[0]
        inp = ["-ss", "45", "-i", str(g)]
        vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,eq=brightness=-0.05,subtitles={rel}:fontsdir=fonts"
    else:
        inp = ["-f", "lavfi", "-i", "color=c=0x334455:s=1080x1920"]
        vf = f"subtitles={rel}:fontsdir=fonts"
    p = subprocess.run([C.FFMPEG, "-v", "error", "-y", *inp, "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out)],
                       cwd=C.APP, capture_output=True, text=True)
    ass.unlink(missing_ok=True)
    if p.returncode != 0:
        raise P.PipelineError(p.stderr[-400:])
    for old in sorted(C.THUMBS.glob("prev_*.jpg"))[:-12]:
        old.unlink(missing_ok=True)
    return "/media/data/thumbs/" + out.name


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- responses
    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def err(self, msg, code=400):
        self.send_json({"error": msg}, code)

    def send_file(self, path: Path, cache=False):
        if not path.exists() or not path.is_file():
            return self.err("not found", 404)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".ttf":
            ctype = "font/ttf"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = size - int(m.group(2))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def save_body(self, dest: Path):
        n = int(self.headers.get("Content-Length") or 0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        return n

    # -- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._get()
        except PermissionError:
            self.err("forbidden", 403)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:
            traceback.print_exc()
            self.err(f"Server error: {e}", 500)

    def _get(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        if p in ("/", "/index.html"):
            return self.send_file(C.STATIC / "index.html")
        if p.startswith("/static/"):
            return self.send_file(safe(C.STATIC, p[8:]))
        if p.startswith("/fonts/"):
            return self.send_file(safe(C.FONTS, p[7:]), cache=True)
        if p.startswith("/media/export/"):
            return self.send_file(safe(C.EXPORT, p[14:]))
        if p.startswith("/media/data/"):
            return self.send_file(safe(C.DATA, p[12:]))
        if p == "/api/state":
            logo = C.BRAND / "clipforge-logo.png"
            return self.send_json({"gameplay": gameplay_list(), "presets": captions.PRESETS,
                                   "fonts": list(captions.FONTS), "positions": list(captions.POSITIONS) + ["auto"],
                                   "defaults": DEFAULT_SETTINGS, "cookies": C.COOKIES.exists(),
                                   "music": [{"name": m.name, "size_mb": round(m.stat().st_size / 1e6, 1)}
                                             for m in R.music_files()],
                                   "logo": f"/api/brand-logo?v={int(logo.stat().st_mtime)}" if logo.exists() else None})
        if p == "/api/brand-logo":
            return self.send_file(C.BRAND / "clipforge-logo.png")
        if p.startswith("/api/gameplay-thumb/"):
            return self.send_file(gameplay_thumb(safe(C.GAMEPLAY, p[20:]).name))
        if p == "/api/jobs":
            with LOCK:
                lst = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
                return self.send_json([summary(j) for j in lst])
        m = re.match(r"^/api/jobs/(\w+)$", p)
        if m:
            j = JOBS.get(m.group(1))
            return self.send_json(j) if j else self.err("no such job", 404)
        self.err("not found", 404)

    def do_POST(self):
        p = unquote(urlparse(self.path).path)
        try:
            if p == "/api/upload":
                name = Path(unquote(self.headers.get("X-Filename", "video.mp4"))).name
                ext = Path(name).suffix.lower()
                if ext not in C.VIDEO_EXT:
                    return self.err("Unsupported file type")
                uid = uuid.uuid4().hex[:10]
                self.save_body(C.UPLOADS / uid / f"source{ext}")
                (C.UPLOADS / uid / "title.txt").write_text(Path(name).stem, encoding="utf-8")
                return self.send_json({"upload_id": uid, "name": name})
            if p == "/api/gameplay":
                name = Path(unquote(self.headers.get("X-Filename", "gameplay.mp4"))).name
                if Path(name).suffix.lower() not in C.VIDEO_EXT:
                    return self.err("Unsupported file type")
                dest = C.GAMEPLAY / name
                k = 1
                while dest.exists():
                    dest = C.GAMEPLAY / f"{Path(name).stem} ({k}){Path(name).suffix}"
                    k += 1
                self.save_body(dest)
                return self.send_json({"ok": True, "name": dest.name})
            if p == "/api/music":
                name = Path(unquote(self.headers.get("X-Filename", "music.mp3"))).name
                if Path(name).suffix.lower() not in C.AUDIO_EXT:
                    return self.err("Unsupported audio type (mp3, wav, m4a, aac, ogg, flac)")
                dest = C.MUSIC / name
                k = 1
                while dest.exists():
                    dest = C.MUSIC / f"{Path(name).stem} ({k}){Path(name).suffix}"
                    k += 1
                self.save_body(dest)
                return self.send_json({"ok": True, "name": dest.name})
            if p == "/api/brand-logo":
                tmp = C.BRAND / "logo-upload.tmp"
                self.save_body(tmp)
                r = subprocess.run([C.FFMPEG, "-v", "error", "-y", "-i", str(tmp), "-vf",
                                    "scale='min(600,iw)':-1,format=rgba", "-frames:v", "1",
                                    str(C.BRAND / "clipforge-logo.png")], capture_output=True, text=True)
                tmp.unlink(missing_ok=True)
                if r.returncode != 0:
                    return self.err("Couldn't read that image — use PNG or JPG.")
                return self.send_json({"ok": True})
            if p == "/api/style-preview":
                return self.send_json({"url": style_preview(self.body_json())})
            if p == "/api/live":
                b = self.body_json()
                url = (b.get("url") or "").strip()
                if not P.supported_url(url):
                    return self.err("Use a Twitch, Kick or YouTube live link.")
                if any(s.is_alive() for s in LIVE.values()):
                    return self.err("A live session is already running — stop it first.")
                jid = uuid.uuid4().hex[:8]
                chan = __import__("livechat").channel_of(url, P.platform(url))
                job = {"id": jid, "type": "live", "created": time.time(), "source": {"type": "url", "url": url},
                       "title": f"LIVE · {chan or P.platform(url)}", "status": "live", "stage": "Starting…", "log": [],
                       "error": None, "clips": [], "outputs": {},
                       "settings": {**DEFAULT_SETTINGS, **(b.get("settings") or {}), "mode": "stream"},
                       "live": {"state": "starting", "platform": P.platform(url), "recorded": 0, "chat_rate": 0,
                                "spark": [], "started": time.time()}}
                JOBS[jid] = job
                save(job)
                sess = LV.LiveSession(job, save, lambda m, j=job: log(j, m))
                LIVE[jid] = sess
                sess.start()
                return self.send_json(job)
            if p == "/api/jobs":
                b = self.body_json()
                src = b.get("source") or {}
                if src.get("type") == "url":
                    if not P.supported_url(src.get("url", "")):
                        return self.err("Use a YouTube, Twitch or Kick link.")
                elif src.get("type") == "upload":
                    if not (C.UPLOADS / src.get("upload_id", "_")).exists():
                        return self.err("Upload not found.")
                else:
                    return self.err("Add a YouTube link or upload a video.")
                jid = uuid.uuid4().hex[:8]
                title = ""
                if src["type"] == "upload":
                    t = C.UPLOADS / src["upload_id"] / "title.txt"
                    title = t.read_text(encoding="utf-8") if t.exists() else ""
                job = {"id": jid, "created": time.time(), "source": src, "title": title, "status": "queued",
                       "stage": "Queued", "log": [], "error": None, "clips": [], "outputs": {},
                       "settings": {**DEFAULT_SETTINGS, **(b.get("settings") or {})}}
                JOBS[jid] = job
                save(job)
                WORK.put((jid, "analyze"))
                return self.send_json(job)
            m = re.match(r"^/api/jobs/(\w+)/(\w+)$", p)
            if m:
                job = JOBS.get(m.group(1))
                if not job:
                    return self.err("no such job", 404)
                act = m.group(2)
                if act == "stop":
                    sess = LIVE.get(job["id"])
                    if sess and sess.is_alive():
                        sess.stop()
                        job["live"]["state"] = "stopping"
                        log(job, "Stopping — finishing the last chunk…")
                    return self.send_json(job)
                if job.get("type") == "live" and act in ("render", "retry", "meta", "detect"):
                    return self.err("Live clips are rendered automatically as they happen.")
                b = self.body_json()
                if job["status"] in ("queued", "working") and act in ("render", "update", "retry"):
                    return self.err("This job is busy — wait for it to finish.")
                if act in ("update", "render"):
                    with LOCK:
                        if "settings" in b:
                            job["settings"] = {**job["settings"], **b["settings"]}
                        if "clips" in b:
                            byid = {c["id"]: c for c in job["clips"]}
                            for c in b["clips"]:
                                if c.get("id") in byid:
                                    t = byid[c["id"]]
                                    t["selected"] = bool(c.get("selected", t["selected"]))
                                    t["title"] = str(c.get("title", t["title"]))[:100]
                                    t["hook_text"] = str(c.get("hook_text", t.get("hook_text", "")))[:60]
                                    t["tease"] = str(c.get("tease", t.get("tease", "")))[:160]
                                    t["start"] = round(float(c.get("start", t["start"])), 2)
                                    t["end"] = round(max(float(c.get("end", t["end"])), t["start"] + 3), 2)
                        save(job)
                    if act == "render":
                        if not any(c.get("selected") for c in job["clips"]):
                            return self.err("Select at least one clip.")
                        job["status"] = "queued"
                        for c in job["clips"]:
                            if c.get("selected"):
                                o = job["outputs"].setdefault(c["id"], {})
                                if not (o.get("status") == "done" and o.get("sig") == sig(job, c)):
                                    o["status"] = "queued"
                        save(job)
                        WORK.put((job["id"], "render"))
                    return self.send_json(job)
                if act == "retry":
                    job["status"] = "queued"
                    save(job)
                    WORK.put((job["id"], "render" if job["clips"] else "analyze"))
                    return self.send_json(job)
                if act == "still":
                    c = next((c for c in job["clips"] if c["id"] == b.get("clip")), None)
                    if not c:
                        return self.err("no such clip")
                    d = source_dir(job)
                    tmp = dict(job["settings"], **(b.get("settings") or {}))
                    out = C.JOBS / job["id"] / f"still_{c['id']}_{uuid.uuid4().hex[:5]}.jpg"
                    for old in (C.JOBS / job["id"]).glob(f"still_{c['id']}_*.jpg"):
                        old.unlink(missing_ok=True)
                    R.render_clip(C.JOBS / job["id"], find_source(d), job["info"], P.load_words(d / "words.json"),
                                  c, tmp, job["clips"].index(c), out, still_at=float(b.get("at", 3)))
                    return self.send_json({"url": "/media/data/" + out.relative_to(C.DATA).as_posix()})
                if act == "detect":
                    if job["status"] in ("queued", "working"):
                        return self.err("This job is busy — wait for it to finish.")
                    run_detect(job)
                    return self.send_json(job)
                if act == "meta":
                    if job["status"] in ("queued", "working"):
                        return self.err("This job is busy — wait for it to finish.")
                    if not any(o.get("status") == "done" for o in job["outputs"].values()):
                        return self.err("Render some clips first.")
                    job["status"] = "queued"
                    save(job)
                    WORK.put((job["id"], "meta"))
                    return self.send_json(job)
                if act == "delete":
                    sess = LIVE.pop(job["id"], None)
                    if sess and sess.is_alive():
                        sess.stop()
                        sess.join(timeout=20)
                    JOBS.pop(job["id"], None)
                    shutil.rmtree(C.JOBS / job["id"], ignore_errors=True)
                    return self.send_json({"ok": True})
            self.err("not found", 404)
        except P.PipelineError as e:
            self.err(str(e), 422)
        except Exception as e:
            traceback.print_exc()
            self.err(f"Server error: {e}", 500)

    def do_DELETE(self):
        p = unquote(urlparse(self.path).path)
        if p.startswith("/api/gameplay/"):
            f = safe(C.GAMEPLAY, p[14:])
            if f.exists():
                trash = C.DATA / "gameplay-trash"
                trash.mkdir(exist_ok=True)
                shutil.move(str(f), str(trash / f.name))      # recoverable, not deleted
            return self.send_json({"ok": True})
        if p.startswith("/api/music/"):
            f = safe(C.MUSIC, p[11:])
            if f.exists():
                trash = C.DATA / "music-trash"
                trash.mkdir(exist_ok=True)
                shutil.move(str(f), str(trash / f.name))      # recoverable, not deleted
            return self.send_json({"ok": True})
        if p == "/api/brand-logo":
            f = C.BRAND / "clipforge-logo.png"
            if f.exists():
                f.replace(C.DATA / "logo-removed.png")       # recoverable
            return self.send_json({"ok": True})
        self.err("not found", 404)


def safe(base: Path, rel):
    p = (base / rel).resolve()
    if base.resolve() not in p.parents and p != base.resolve():
        raise PermissionError(rel)
    return p


def summary(j):
    done = sum(1 for o in j.get("outputs", {}).values() if o.get("status") == "done")
    return {"id": j["id"], "title": j.get("title") or j["source"].get("url") or "Upload", "status": j["status"],
            "stage": j.get("stage"), "created": j["created"], "clips": len(j.get("clips", [])), "rendered": done}


if __name__ == "__main__":
    load_all()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"ClipForge running at http://localhost:{PORT}", flush=True)
    srv.serve_forever()
