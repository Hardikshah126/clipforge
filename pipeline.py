"""Fetch -> transcribe -> sentences -> pick moments."""
import json, re, shutil, subprocess
from pathlib import Path

import config as C


class PipelineError(Exception):
    pass


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if p.returncode != 0:
        raise PipelineError((p.stderr or p.stdout)[-1500:])
    return p.stdout


def probe(path):
    out = run([C.FFPROBE, "-v", "error", "-show_entries",
               "stream=codec_type,width,height:format=duration", "-of", "json", str(path)])
    j = json.loads(out)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    a = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    return {"duration": float(j["format"].get("duration", 0)),
            "width": v and v.get("width"), "height": v and v.get("height"), "has_audio": a}


def youtube_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def fetch_youtube(url, dest_dir: Path, log):
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "source.mp4"
    if out.exists() and out.stat().st_size > 0:
        log("Source already downloaded — reusing it.")
        return out
    cmd = [C.YTDLP, "--no-playlist", "-S", "vcodec:h264,res:1080,acodec:m4a",
           "--merge-output-format", "mp4", "-o", str(dest_dir / "source.%(ext)s")]
    if C.COOKIES.exists():
        cmd += ["--cookies", str(C.COOKIES)]
        log("Using cookies.txt for YouTube.")
    cmd.append(url)
    log("Downloading from YouTube…")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not out.exists():
        err = p.stderr or p.stdout
        if "not a bot" in err or "Sign in to confirm" in err:
            raise PipelineError(
                "YouTube blocked the download (\"Sign in to confirm you're not a bot\"). "
                "This is a temporary block on this internet connection. Download the video "
                "yourself and use “Upload a file”, switch network, or put a cookies.txt in the clipforge folder.")
        raise PipelineError("yt-dlp failed: " + err[-600:])
    try:
        meta = run([C.YTDLP, "--skip-download", "--print", "%(title)s", url]).strip()
    except Exception:
        meta = ""
    if meta:
        (dest_dir / "title.txt").write_text(meta, encoding="utf-8")
    return out


def transcribe(src: Path, work: Path, log):
    words_json = work / "words.json"
    if words_json.exists():
        log("Transcript already exists — reusing it.")
        return load_words(words_json)
    wav = work / "audio.wav"
    log("Extracting audio…")
    run([C.FFMPEG, "-v", "error", "-y", *C.FF_THREADS, "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)])
    log("Transcribing word by word (whisper on GPU)…")
    run([str(C.WHISPER_CLI), "-m", C.WHISPER_MODEL, "-f", str(wav), "-ml", "1", "-sow", "-oj",
         "-of", str(work / "words"), "-l", "en"])
    wav.unlink(missing_ok=True)
    return load_words(words_json)


def load_words(path):
    raw = json.load(open(path, encoding="utf-8"))["transcription"]
    words = []
    for x in raw:
        t = x["text"]
        s = t.strip()
        if not s or s.startswith("[") or s.startswith("(") or s.startswith("♪"):
            continue
        a, b = x["offsets"]["from"] / 1000, x["offsets"]["to"] / 1000
        if words and not t.startswith(" "):
            words[-1]["e"] = b
            words[-1]["w"] += s
        else:
            words.append({"s": a, "e": b, "w": s})
    # sanity: monotonic, non-zero
    for i, w in enumerate(words):
        if w["e"] <= w["s"]:
            w["e"] = w["s"] + 0.12
        if i and w["s"] < words[i - 1]["s"]:
            w["s"] = words[i - 1]["s"]
    return words


def sentences(words):
    """Split words into sentences; long run-ons get split at commas."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        end_p = w["w"][-1] in ".?!"
        pause = nxt and nxt["s"] - w["e"] > 0.9
        dur = w["e"] - words[cur[0]]["s"]
        comma_split = dur > 18 and w["w"][-1] in ",;:"
        if not nxt or end_p or pause or comma_split:
            out.append({"a": cur[0], "b": cur[-1], "s": words[cur[0]]["s"], "e": w["e"],
                        "text": " ".join(words[k]["w"] for k in cur)})
            cur = []
    return out


# ---------------------------------------------------------------- moment picking
HOOK = set("""you your never always secret actually crazy insane wild biggest best worst only first nobody
everyone everything nothing free money million billion why how what if imagine literally finally truth
mistake stop wrong hate love dead kill steal broke rich fat viral hidden real""".split())
OUTRO = re.compile(r"subscribe|thanks for watching|thank you all|like (on )?this video|link in the description|"
                   r"sponsor|patreon|twitch\.tv|follow me|see you (guys )?in the next|in today's video", re.I)


def fit_window(sents, i, j, lo, hi):
    """Grow/shrink sentence span [i..j] so its duration lands in [lo, hi]."""
    dur = lambda a, b: sents[b]["e"] - sents[a]["s"]
    while dur(i, j) < lo and j + 1 < len(sents) and dur(i, j + 1) <= hi + 2:
        j += 1
    while dur(i, j) > hi and j > i:
        j -= 1
    return i, j


def heuristic_moments(sents, n, lo, hi):
    cands = []
    for i in range(len(sents)):
        j = i
        while j + 1 < len(sents) and sents[j + 1]["e"] - sents[i]["s"] <= hi:
            j += 1
        d = sents[j]["e"] - sents[i]["s"]
        if d < lo:
            continue
        text = " ".join(s["text"] for s in sents[i:j + 1])
        if OUTRO.search(text):
            continue
        toks = re.findall(r"[a-z']+", text.lower())
        first = re.findall(r"[a-z']+", sents[i]["text"].lower())
        score = sum(t in HOOK for t in toks) / max(len(toks), 1) * 10
        score += sum(t in HOOK for t in first) * 0.6
        score += text.count("?") * 0.5 + text.count("!") * 0.4
        score += len(re.findall(r"\d", text)) * 0.1
        score += len(toks) / d * 0.15                     # energy: words per second
        if first and first[0] in {"and", "but", "so", "or", "because", "which"}:
            score -= 0.8                                  # weak cold open
        cands.append((score, i, j))
    cands.sort(reverse=True)
    picked = []
    for score, i, j in cands:
        if all(j < a or i > b for _, a, b in picked):
            picked.append((score, i, j))
        if len(picked) >= n:
            break
    out = []
    for score, i, j in sorted(picked, key=lambda x: x[1]):
        first = sents[i]["text"]
        out.append({"first": i, "last": j, "title": first[:60].rstrip(",. ") + ("…" if len(first) > 60 else ""),
                    "hook": "Auto-picked (keyword + energy score)"})
    return out


def ai_moments(sents, n, lo, hi, log, title=""):
    lines = "\n".join(f"[{k}] ({s['s']:.1f}-{s['e']:.1f}s) {s['text']}" for k, s in enumerate(sents))
    prompt = f"""You are a top short-form video editor (TikTok / YouTube Shorts / Reels).
Video title: {title or 'unknown'}
Below is the full transcript split into numbered sentences with timestamps.

Pick the {n} BEST moments to post as standalone vertical clips. Rules:
- Each clip is a contiguous span of sentences [first..last], total length between {lo} and {hi} seconds (last.end - first.start).
- It must make sense with zero context: open on a strong hook (a surprising claim, question, or vivid detail) — never open on "And", "So", "But" mid-thought.
- It must end on a complete, satisfying thought (payoff/punchline), not mid-explanation.
- No overlaps. Skip intros, sponsor reads, "like and subscribe" outros.
- Rank from strongest to weakest.
- title: a punchy Shorts title (max 60 chars, can use 1 emoji). hook: one short line on why it works.

Return ONLY a JSON array, no prose, no code fences:
[{{"first": 12, "last": 19, "title": "...", "hook": "..."}}]

TRANSCRIPT:
{lines}
"""
    log("Asking Claude to pick the most engaging moments…")
    p = subprocess.run([C.CLAUDE, "-p", "--model", "sonnet", "--tools", ""], input=prompt,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    txt = p.stdout.strip()
    m = re.search(r"\[.*\]", txt, re.S)
    if p.returncode != 0 or not m:
        raise PipelineError("Claude picker failed: " + (p.stderr or txt)[-300:])
    picks = json.loads(m.group(0))
    out = []
    for x in picks:
        i, j = int(x["first"]), int(x["last"])
        if not (0 <= i <= j < len(sents)):
            continue
        out.append({"first": i, "last": j, "title": str(x.get("title", ""))[:80], "hook": str(x.get("hook", ""))})
    if not out:
        raise PipelineError("Claude returned no usable moments")
    return out


def pick_moments(words, n, lo, hi, use_ai, log, title=""):
    sents = sentences(words)
    total = n + 3          # a few spares the user can swap in
    picks = None
    if use_ai:
        try:
            picks = ai_moments(sents, total, lo, hi, log, title)
        except Exception as e:  # fall back, never fail the job here
            log(f"AI picker unavailable ({str(e)[:120]}) — using the built-in scorer.")
    if not picks:
        log("Scoring moments with the built-in picker…")
        picks = heuristic_moments(sents, total, lo, hi)
    clips, used = [], []
    for k, p in enumerate(picks):
        i, j = fit_window(sents, p["first"], p["last"], lo, hi)
        if any(not (j < a or i > b) for a, b in used):
            continue
        used.append((i, j))
        clips.append({
            "id": f"c{k + 1}",
            "start": round(sents[i]["s"], 2),
            "end": round(sents[j]["e"], 2),
            "title": p["title"],
            "hook": p["hook"],
            "text": " ".join(s["text"] for s in sents[i:j + 1]),
            "selected": len(clips) < n,
        })
    return clips


# ---------------------------------------------------------------- upload metadata
def _tags_from(text, base):
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text)
    stop = {"the", "and", "you", "your", "this", "that", "with", "will", "are", "for", "can", "have", "just", "also",
            "they", "their", "from", "into", "what", "when", "which", "there", "about", "going", "actually", "even",
            "like", "not", "but", "was", "were", "been", "its", "it's", "our", "out", "all", "get", "got", "game"}
    seen, out = set(), []
    for w in words:
        k = w.lower()
        if k in stop or k in seen:
            continue
        seen.add(k)
        out.append("#" + k)
    return (base + out)[:6]


def fallback_meta(clip, source_title, source_url):
    title = clip["title"][:90]
    if "#shorts" not in title.lower() and len(title) < 80:
        title += " #shorts"
    first = re.split(r"(?<=[.!?])\s", clip["text"].strip())
    blurb = " ".join(first[:2])[:220]
    credit = f"\n\n🎥 Credit: {source_title or 'original video'}" + (f" – {source_url}" if source_url else "")
    tags = _tags_from(clip["title"] + " " + clip["text"], ["#shorts"])
    return {"title": title, "description": blurb + credit + "\n\n" + " ".join(tags), "hashtags": tags}


def ai_meta(clips, source_title, source_url, log):
    """One Claude call writes a YouTube Shorts title + description + hashtags for every clip."""
    items = "\n\n".join(f'[{c["id"]}] working title: {c["title"]}\ntranscript: {c["text"][:1500]}' for c in clips)
    prompt = f"""You write YouTube Shorts metadata for a gaming clips channel (mostly GTA 6, other games too).
Source video: {source_title or 'unknown'} {source_url or ''}

For EACH clip below write:
- title: max 70 characters, curiosity-driven and specific to what is said in the clip (not clickbait lies), 1 emoji max, end with " #shorts".
- description: 2-3 short punchy sentences in casual creator voice that tease the clip's best detail and end with a question to drive comments. Do NOT include hashtags or credit lines in it.
- hashtags: 5 relevant lowercase hashtags including #shorts (e.g. #gta6 #gaming).

Return ONLY a JSON object keyed by clip id, no prose, no code fences:
{{"c1": {{"title": "...", "description": "...", "hashtags": ["#shorts", "..."]}}}}

CLIPS:
{items}
"""
    log("Writing titles & descriptions with Claude…")
    p = subprocess.run([C.CLAUDE, "-p", "--model", "sonnet", "--tools", ""], input=prompt,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    m = re.search(r"\{.*\}", p.stdout, re.S)
    if p.returncode != 0 or not m:
        raise PipelineError("Claude metadata failed: " + (p.stderr or p.stdout)[-300:])
    data = json.loads(m.group(0))
    credit = f"🎥 Credit: {source_title or 'original video'}" + (f" – {source_url}" if source_url else "")
    out = {}
    for c in clips:
        x = data.get(c["id"])
        if not x:
            continue
        tags = [t if t.startswith("#") else "#" + t for t in x.get("hashtags", [])][:6] or ["#shorts"]
        if "#shorts" not in [t.lower() for t in tags]:
            tags.insert(0, "#shorts")
        desc = str(x.get("description", "")).strip()
        out[c["id"]] = {"title": str(x.get("title", c["title"]))[:100],
                        "description": f"{desc}\n\n{credit}\n\n{' '.join(tags)}", "hashtags": tags}
    return out


def make_meta(clips, source_title, source_url, use_ai, log):
    meta = {}
    if use_ai:
        try:
            meta = ai_meta(clips, source_title, source_url, log)
        except Exception as e:
            log(f"AI titles unavailable ({str(e)[:120]}) — using templates.")
    for c in clips:
        if c["id"] not in meta:
            meta[c["id"]] = fallback_meta(c, source_title, source_url)
    return meta
