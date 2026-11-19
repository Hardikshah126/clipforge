"""Live mode: watch a stream as it airs and auto-clip hype moments.

record (ffmpeg, 30 s chunks) -> per chunk: loudness + whisper words -> hype curve every few seconds
-> spike = cut the moment from the chunks, AI title it, render it with the project's captions/polish.
Everything is on the RECORDING clock: seconds since recording started.
"""
import json
import re
import shutil
import subprocess
import threading
import time
import traceback
import wave
from pathlib import Path

import numpy as np

import config as C
import effects as FX
import facecam
import highlights as HL
import livechat
import pipeline as P
import render as R

SEG = 30                                   # seconds per recorded chunk
SENS = {"low": 3.2, "medium": 2.4, "high": 1.7}
SPAM = re.compile(r"\b(kekw|lul|lmao+|lmfao|omegalul|pog(champ|gers)?|w+|clip (it|that)|holy|no way|"
                  r"wtf|omg|bro+|ahaha+|haha+|xd+|💀+|😂+|🔥+)\b", re.I)


class LiveSession(threading.Thread):
    def __init__(self, job, save, log):
        super().__init__(daemon=True)
        self.job, self.save, self.log = job, save, log
        self.stop_evt = threading.Event()
        self.url = job["source"]["url"]
        self.platform = P.platform(self.url)
        self.dir = C.JOBS / job["id"] / "live"
        self.segdir = self.dir / "segments"
        self.segdir.mkdir(parents=True, exist_ok=True)
        self.segs = []                     # [{"file", "start", "end"}] recording clock
        self.processed = 0                 # seconds of recording analysed
        self.loud = []                     # dB every 0.5 s on the recording clock
        self.words = []
        self.chat = np.zeros(12 * 3600)    # messages per second (up to a 12 h stream)
        self.spam = np.zeros(12 * 3600)    # hype-emote messages per second
        self.rec_wall = None
        self.last_peak = -999
        self.proc = None
        self.run_no = 0
        self.offset = 0.0                  # recording time already covered by earlier ffmpeg runs
        self.export = C.EXPORT / f"live-{(livechat.channel_of(self.url, self.platform) or 'stream')}-{job['id']}"

    # ---------------------------------------------------------------- lifecycle
    def stop(self):
        self.stop_evt.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except Exception:
                self.proc.terminate()

    def run(self):
        L = self.job["live"]
        try:
            livechat.start(self.platform, self.url, self.on_chat, self.stop_evt, self.log, self.dir)
            fails = 0
            while not self.stop_evt.is_set():
                if self.proc is None or self.proc.poll() is not None:
                    if self.proc is not None:
                        self.collect()             # keep the chunks from the run that just ended
                        fails += 1
                        if fails > 4:
                            self.log("Stream ended or went offline.")
                            break
                        self.log("Recorder reconnecting…")
                        self.stop_evt.wait(5)
                    if not self.start_recorder():
                        if not self.segs:                  # not live yet: keep waiting until it starts
                            self.job["live"]["state"] = "waiting"
                            self.save(self.job)
                            self.stop_evt.wait(30)
                            continue
                        fails += 1
                        if fails > 4:
                            break
                        self.stop_evt.wait(10)
                        continue
                try:
                    self.collect()
                    if self.process_new():
                        fails = 0
                        self.detect()
                    self.errors = 0
                except Exception as e:
                    self.errors = getattr(self, "errors", 0) + 1
                    self.note_error(e)
                    if self.errors > 8:
                        raise
                L["recorded"] = round(self.recorded_now(), 1)
                L["chat_rate"] = round(float(self.chat[max(0, int(self.recorded_now()) - 10): int(self.recorded_now())].sum()) / 10, 1)
                self.save(self.job)
                self.stop_evt.wait(3)
        except Exception as e:
            self.note_error(e)
            self.job["error"] = f"Live mode stopped: {e}"
        finally:
            self.stop()
            if self.proc:
                try:
                    self.proc.wait(timeout=10)
                except Exception:
                    self.proc.kill()
            self.collect()
            if self.process_new():
                self.detect(final=True)
            shutil.rmtree(self.segdir, ignore_errors=True)  # free the rolling buffer once the session ends
            L["state"] = "ended"
            self.job["status"] = "done"
            self.log(f"Live session finished — {len(self.job['clips'])} clips.")
            self.save(self.job)

    def note_error(self, e):
        with open(self.dir / "error.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + "".join(traceback.format_exception(e)) + "\n")
        if getattr(e, "winerror", None) == 4551:
            self.log("⚠ Windows Smart App Control blocked a program ClipForge uses — retrying. "
                     "Details in live/error.log.")
        else:
            self.log(f"⚠ Live hiccup: {str(e)[:160]} — continuing.")

    def tool(self, name, cmd):
        """Run an external tool; Windows Application Control blocks (WinError 4551) are retried, then skipped."""
        for attempt in range(3):
            try:
                return subprocess.run(cmd, capture_output=True)
            except OSError as e:
                if getattr(e, "winerror", None) != 4551:
                    raise
                if attempt == 2:
                    self.log(f"⚠ Windows blocked {name} (Smart App Control) — this chunk skips {name}.")
                    with open(self.dir / "error.log", "a", encoding="utf-8") as f:
                        f.write(time.strftime("%H:%M:%S ") + f"blocked: {name}: {cmd[0]}\n")
                    return None
                time.sleep(2)

    def recorded_now(self):
        return self.offset + (time.time() - self.rec_wall if self.rec_wall else 0)

    # ---------------------------------------------------------------- recording
    def start_recorder(self):
        self.log("Connecting to the live stream…")
        cmd = [C.YTDLP, "-g", "-f", "best[height<=1080]/best", "--no-warnings", self.url]
        if C.COOKIES.exists():
            cmd[1:1] = ["--cookies", str(C.COOKIES)]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        hls = (p.stdout.strip().splitlines() or [""])[0]
        if p.returncode != 0 or not hls.startswith("http"):
            err = p.stderr or ""
            if "not a bot" in err or "Sign in to confirm" in err:
                self.log("YouTube blocked this PC (bot check). Add cookies.txt to the clipforge folder.")
            elif "is offline" in err or "not currently live" in err.lower() or "not live" in err.lower():
                self.log("This channel isn't live right now — waiting…")
            else:
                self.log("Couldn't open the stream: " + err.strip()[-160:])
            return False
        self.offset = self.segs[-1]["end"] if self.segs else 0.0
        self.run_no += 1
        rd = self.segdir / f"run{self.run_no:02d}"
        rd.mkdir(exist_ok=True)
        self.proc = subprocess.Popen(
            [C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-reconnect", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "10", "-i", hls, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
             "-f", "segment", "-segment_time", str(SEG), "-reset_timestamps", "1",
             "-segment_list", str(rd / "list.csv"), "-segment_list_type", "csv", str(rd / "seg_%05d.ts")],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.rec_wall = time.time()
        self.job["live"]["state"] = "recording"
        self.log(f"🔴 Recording the stream ({self.platform}).")
        return True

    def collect(self):
        """Read finished chunks from every run's segment list onto the recording clock."""
        known = {s["file"] for s in self.segs}
        for rd in sorted(self.segdir.glob("run*")):
            lst = rd / "list.csv"
            if not lst.exists():
                continue
            for line in lst.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split(",")
                if len(parts) < 3:
                    continue
                f = str(rd / parts[0])
                if f in known:
                    continue
                dur = float(parts[2]) - float(parts[1])
                start = self.segs[-1]["end"] if self.segs else 0.0
                self.segs.append({"file": f, "start": start, "end": start + dur, "done": False})
                known.add(f)

    # ---------------------------------------------------------------- analysis
    def on_chat(self, text):
        t = int(self.recorded_now()) - 4          # viewers react a few seconds after the moment
        if 0 <= t < len(self.chat):
            self.chat[t] += 1
            if text and SPAM.search(text):
                self.spam[t] += 1

    def process_new(self):
        did = False
        for s in self.segs:
            if s["done"] or not Path(s["file"]).exists():
                continue
            wav = Path(s["file"]).with_suffix(".wav")
            r = self.tool("ffmpeg", [C.FFMPEG, "-v", "error", "-y", *C.FF_THREADS, "-i", s["file"], "-vn", "-ac", "1",
                                     "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
            s["done"] = True
            if r is None or r.returncode != 0 or not wav.exists():
                self.processed = s["end"]
                continue
            with wave.open(str(wav), "rb") as w:                       # loudness, 0.5 s steps
                n = 8000
                need = int(s["start"] * 2)
                if len(self.loud) < need:
                    self.loud += [-60.0] * (need - len(self.loud))
                while True:
                    buf = w.readframes(n)
                    if not buf:
                        break
                    a = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                    self.loud.append(float(20 * np.log10(np.sqrt(np.mean(a * a)) + 1e-6)))
            out = wav.with_suffix("")
            r = self.tool("whisper", [str(C.WHISPER_CLI), "-m", C.WHISPER_MODEL, "-f", str(wav), "-ml", "1", "-sow",
                                      "-oj", "-of", str(out), "-l", "en"])
            jf = out.with_suffix(".json")
            if r is not None and r.returncode == 0 and jf.exists():
                for w in P.load_words(jf):
                    self.words.append({"s": w["s"] + s["start"], "e": w["e"] + s["start"], "w": w["w"]})
                jf.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
            self.processed = s["end"]
            did = True
        # keep disk small: drop chunks older than 6 minutes (clips are cut well before that)
        for s in self.segs:
            if s["done"] and s["end"] < self.processed - 360 and Path(s["file"]).exists():
                Path(s["file"]).unlink(missing_ok=True)
        return did

    def curve(self):
        n = int(self.processed) + 1
        L = HL.loud_signal({"step": 0.5, "db": self.loud}, n)
        Rc = HL.react_signal(self.words[-4000:], Path("nonexistent"), n)
        score = 1.0 * HL._smooth(L, 3) + 0.9 * Rc
        chat = self.chat[:n]
        if chat.sum() > 30:
            score += 1.3 * HL._z(HL._smooth(chat, 5)) + 0.8 * HL._smooth(np.clip(self.spam[:n], 0, 30), 5) / 3
        return HL._smooth(score, 4), L, chat

    def detect(self, final=False):
        st = self.job["settings"]
        if self.processed < 120:                                  # need a baseline first
            return
        if not self.job["live"].get("layout_done"):
            self.pick_layout()
        curve, L, chat = self.curve()
        self.job["live"]["spark"] = [round(float(x), 2) for x in curve[-180:]]
        thr = SENS.get(st.get("live_sens", "medium"), 2.4)
        lo, hi = float(st.get("min_len", 30)), float(st.get("max_len", 40))
        now = int(self.processed)
        a, b = max(60, now - 50), now - (2 if final else 10)     # leave ~10 s after a peak for the payoff
        if b <= a:
            return
        t = a + int(np.argmax(curve[a:b]))
        if curve[t] < thr or t - self.last_peak < 75:
            return
        if curve[t] < curve[max(0, t - 20): t + 10].max() - 1e-6:   # must be the local summit
            return
        self.last_peak = t
        sig = []
        if L[t] > 2:
            sig.append(f"audio spike ×{L[t]:.1f}")
        cz = HL._z(HL._smooth(chat, 5)) if chat.sum() > 30 else None
        if cz is not None and cz[t] > 1.5:
            sig.append(f"chat burst ×{cz[t]:.1f}")
        if self.spam[max(0, t - 8): t + 8].sum() > 3:
            sig.append("chat spamming hype emotes")
        self.make_clip(t, float(curve[t]), sig or ["energy peak"], lo, hi)

    def pick_layout(self):
        st = self.job["settings"]
        self.job["live"]["layout_done"] = True
        if st.get("layout") not in ("auto", "stream"):
            return
        probe = self.concat(0, min(self.processed, 120), self.dir / "probe.mp4")
        if not probe:
            return
        try:
            r = facecam.detect(probe, P.probe(probe)["duration"], C.JOBS / self.job["id"], self.log)
            st["face_box"], st["game_box"] = r["face_box"], r["game_box"]
            self.job["facecam"] = {"found": r["found"], "v": int(time.time()),
                                   "frame": "/media/data/" + r["frame"].relative_to(C.DATA).as_posix()}
            if st.get("layout") == "auto":
                st["layout"] = "stream" if r["found"] else "fit"
                self.log("Layout: Streamer cam (facecam found)." if r["found"] else "Layout: Fit + blur (no facecam).")
        except Exception as e:
            self.log(f"Facecam detection skipped ({str(e)[:80]})")
            if st.get("layout") == "auto":
                st["layout"] = "fit"
        probe.unlink(missing_ok=True)

    # ---------------------------------------------------------------- clipping
    def concat(self, a, b, out: Path):
        parts = [s for s in self.segs if s["end"] > a and s["start"] < b and Path(s["file"]).exists()]
        if not parts:
            return None
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{Path(s['file']).as_posix()}'\n" for s in parts), encoding="utf-8")
        r = subprocess.run([C.FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", "-bsf:a", "aac_adtstoasc", str(out)], capture_output=True)
        lst.unlink(missing_ok=True)
        if r.returncode != 0 or not out.exists():
            return None
        out.with_suffix(".off").write_text(str(parts[0]["start"]), encoding="utf-8")
        return out

    def make_clip(self, peak, hype, signals, lo, hi):
        k = len(self.job["clips"]) + 1
        cid = f"L{k}"
        target = (lo + hi) / 2
        a_t, b_t = peak - (target - 8), peak + 8
        near = [w for w in self.words if a_t - 12 <= w["s"] <= b_t + 6]
        if near:                                         # snap to sentence edges around the peak
            sents = P.sentences(near)
            first = max([s for s in sents if s["s"] <= a_t + 3] or sents[:1], key=lambda s: s["s"])
            last = min([s for s in sents if s["e"] >= b_t - 2] or sents[-1:], key=lambda s: s["e"])
            a_t, b_t = first["s"] - 0.15, last["e"] + 0.35
            if b_t - a_t > hi + 6:
                a_t = b_t - hi
        a_t = max(0.0, a_t)
        b_t = min(self.processed, b_t)
        clip_dir = C.JOBS / self.job["id"]
        src = self.concat(a_t - 1, b_t + 1, self.dir / f"{cid}_src.mp4")
        if not src:
            return
        off = float(src.with_suffix(".off").read_text())
        words = [{"s": w["s"] - off, "e": w["e"] - off, "w": w["w"]} for w in self.words if a_t <= w["s"] < b_t]
        text = " ".join(w["w"] for w in words)
        stamp = time.strftime("%H:%M:%S", time.gmtime(peak))
        self.log(f"⚡ Hype at {stamp} ({', '.join(signals)}) — clipping it.")
        meta = ai_clip_meta(text, signals, self.job.get("title", "")) if self.job["settings"].get("ai_pick", True) else None
        meta = meta or fallback_meta(text, signals)
        clip = {"id": cid, "start": round(a_t - off, 2), "end": round(b_t - off, 2), "src": str(src),
                "at": stamp, "peak": peak, "hype": round(hype, 2), "text": text or "(no speech)", "selected": True,
                "title": meta["title"], "hook": "Live hype: " + ", ".join(signals),
                "score": meta["score"], "reasons": ["⚡ " + s for s in signals] + meta.get("reasons", [])[:2],
                "hook_text": meta["hook_text"], "keywords": meta["keywords"], "emojis": meta["emojis"], "loop": False,
                "tease_at": round(peak - off, 2)}   # cold open on the hype peak itself
        self.job["clips"].append(clip)
        o = self.job["outputs"].setdefault(cid, {"status": "rendering"})
        self.save(self.job)
        self.export.mkdir(parents=True, exist_ok=True)
        self.job["export_dir"] = str(self.export)
        out = self.export / f"{k:02d}-{stamp.replace(':', '')}-{_slug(clip['title'])}.mp4"
        try:
            info = P.probe(src)
            render_words = words if words else [{"s": clip["start"] + 0.2, "e": clip["end"] - 0.2, "w": " "}]
            dur = R.render_clip(clip_dir, src, info, render_words, clip, self.job["settings"], k - 1, out)
            o.update(status="done", file=str(out), dur=dur, sig="live", v=int(time.time()),
                     url="/media/export/" + out.relative_to(C.EXPORT).as_posix(),
                     thumb="/media/export/" + out.with_suffix(".jpg").relative_to(C.EXPORT).as_posix())
            url = self.url
            o["meta"] = P.make_meta([clip], self.job.get("title", ""), url,
                                    bool(self.job["settings"].get("ai_pick", True)), self.log)[cid]
            o["meta_sig"] = "live"
            self.log(f"✓ Clip {k} ready: {clip['title']}")
            src.unlink(missing_ok=True)                    # raw cut no longer needed once rendered
            src.with_suffix(".off").unlink(missing_ok=True)
        except Exception as e:
            o.update(status="error", error=str(e)[-300:])
            self.log(f"Clip {k} failed: {str(e)[:160]}")
        self.save(self.job)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "clip").lower()).strip("-")[:36] or "clip"


def fallback_meta(text, signals):
    return {"title": (P.hook_from(text).title() or "Stream Moment")[:60], "score": 70,
            "hook_text": P.hook_from(text) or "WAIT FOR IT", "keywords": P.keywords_from(text),
            "emojis": P.emojis_from(text), "reasons": []}


def ai_clip_meta(text, signals, stream_title):
    prompt = f"""You title live-stream clips for YouTube Shorts / TikTok.
Stream: {stream_title or 'live stream'}
Why it was clipped: {', '.join(signals)}
Transcript of the clip (may be partial, stream speech is messy):
{text[:2500]}

Return ONLY a JSON object, no prose:
{{"title": "punchy Shorts title, max 60 chars, 1 emoji max",
  "hook_text": "on-screen hook. {P.HOOK_TEXT_RULES.replace('"', "'")}",
  "score": 0-100 virality guess,
  "reasons": ["2 very short reasons"],
  "keywords": ["4-8 words from the transcript worth emphasising"],
  "emojis": [{{"word": "a word from the transcript", "emoji": "one emoji"}}]}}"""
    try:
        p = subprocess.run([C.CLAUDE, "-p", "--model", "sonnet", "--tools", ""], input=prompt, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
        m = re.search(r"\{.*\}", p.stdout, re.S)
        x = json.loads(m.group(0))
        return {"title": str(x.get("title", ""))[:80] or "Stream Moment",
                "hook_text": str(x.get("hook_text", ""))[:48].upper(),
                "score": max(0, min(100, int(x.get("score", 70) or 70))),
                "reasons": [str(r)[:80] for r in x.get("reasons", [])][:3],
                "keywords": [str(k).lower() for k in x.get("keywords", [])][:10],
                "emojis": [e for e in x.get("emojis", []) if isinstance(e, dict) and e.get("word") and e.get("emoji")][:5]}
    except Exception:
        return None
