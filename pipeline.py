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
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def platform(url):
    u = url.lower()
    if "youtu.be/" in u or "youtube.com/" in u:
        return "youtube"
    if "twitch.tv/" in u:
        return "twitch"
    if "kick.com/" in u:
        return "kick"
    return None


def video_id(url):
    """Stable folder name for a link (YouTube id, twitch-<vod>, kick-<id>)."""
    if platform(url) == "youtube":
        return youtube_id(url)
    m = re.search(r"twitch\.tv/videos/(\d+)", url) or re.search(r"twitch\.tv/\w+/clip/([\w-]+)", url) \
        or re.search(r"clips\.twitch\.tv/([\w-]+)", url)
    if m:
        return "twitch-" + m.group(1)
    m = re.search(r"kick\.com/[\w-]+/(?:videos|clips)/([\w-]+)", url) or re.search(r"kick\.com/video/([\w-]+)", url)
    if m:
        return "kick-" + m.group(1)
    return "link-" + re.sub(r"[^a-zA-Z0-9]+", "-", url)[-40:].strip("-")


def supported_url(url):
    return platform(url) is not None


def fetch_youtube(url, dest_dir: Path, log, stream=False):
    """Download any supported link (YouTube / Twitch / Kick). stream=True also grabs the chat replay."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "source.mp4"
    if out.exists() and out.stat().st_size > 0:
        log("Source already downloaded — reusing it.")
        return out
    site = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}.get(platform(url), "the web")
    cmd = [C.YTDLP, "--no-playlist", "-S", "vcodec:h264,res:1080,fps:30,acodec:m4a",
           "--merge-output-format", "mp4", "-o", str(dest_dir / "source.%(ext)s")]
    if stream:   # chat replay powers hype detection (YouTube live_chat, Twitch rechat)
        cmd += ["--write-subs", "--sub-langs", "live_chat,rechat", "--ignore-errors"]
    if C.COOKIES.exists():
        cmd += ["--cookies", str(C.COOKIES)]
        log("Using cookies.txt.")
    cmd.append(url)
    log(f"Downloading from {site}…" + (" (long streams can take a while)" if stream else ""))
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
    loudness_curve(wav, work)                # cheap, reused by stream hype detection
    log("Transcribing word by word (whisper on GPU)…")
    run([str(C.WHISPER_CLI), "-m", C.WHISPER_MODEL, "-f", str(wav), "-ml", "1", "-sow", "-oj",
         "-of", str(work / "words"), "-l", "en"])
    wav.unlink(missing_ok=True)
    return load_words(words_json)


def loudness_curve(wav: Path, work: Path, step=0.5):
    """RMS loudness (dB) every `step` seconds of a 16 kHz mono wav -> loud.json. Streams through the file."""
    import wave
    import numpy as np
    out = []
    with wave.open(str(wav), "rb") as w:
        n = int(w.getframerate() * step)
        while True:
            buf = w.readframes(n)
            if not buf:
                break
            a = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
            out.append(round(float(20 * np.log10(np.sqrt(np.mean(a * a)) + 1e-6)), 1))
    (work / "loud.json").write_text(json.dumps({"step": step, "db": out}), encoding="utf-8")
    return out


def ensure_loudness(src: Path, work: Path, log):
    f = work / "loud.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    wav = work / "audio_loud.wav"
    log("Measuring audio energy…")
    run([C.FFMPEG, "-v", "error", "-y", *C.FF_THREADS, "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)])
    loudness_curve(wav, work)
    wav.unlink(missing_ok=True)
    return json.loads(f.read_text(encoding="utf-8"))


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
    top = max((p[0] for p in picked), default=1) or 1
    for score, i, j in sorted(picked, key=lambda x: x[1]):
        first = sents[i]["text"]
        text = " ".join(s["text"] for s in sents[i:j + 1])
        reasons = []
        ft = re.findall(r"[a-z']+", first.lower())
        if any(t in HOOK for t in ft):
            reasons.append("hook words in the opening line")
        if "?" in first:
            reasons.append("opens with a question")
        if ft and ft[0] in {"and", "but", "so", "or", "because", "which"}:
            reasons.append("weak opening (starts mid-thought)")
        if re.search(r"\d", text):
            reasons.append("specific numbers")
        out.append({"first": i, "last": j, "title": first[:60].rstrip(",. ") + ("…" if len(first) > 60 else ""),
                    "hook": "Auto-picked (keyword + energy score)",
                    "score": int(45 + 45 * score / top), "reasons": reasons or ["steady pacing"],
                    "hook_text": hook_from(first), "keywords": keywords_from(text), "emojis": emojis_from(text),
                    "loop": False})
    return out


STOPWORDS = set("""the a an and or but so to of in on at for with from by is are was were be been being it its it's this
that these those you your i me my we our they them their he she his her as if then than there here what when where
which who whom how why will would can could should just also very really going gonna get got have has had do does did
not no yes all any some more most other into about over after before up down out off again only even like well""".split())
EMOJI_MAP = {"money": "💰", "cash": "💰", "rich": "💰", "dollar": "💰", "car": "🚗", "cars": "🚗", "race": "🏎️",
             "races": "🏎️", "police": "🚨", "cops": "🚨", "cop": "🚨", "dog": "🐶", "dogs": "🐶", "pet": "🐶",
             "pets": "🐶", "poop": "💩", "fire": "🔥", "gun": "🔫", "guns": "🔫", "weapon": "🔫", "crazy": "🤯",
             "insane": "🤯", "map": "🗺️", "world": "🌍", "phone": "📱", "viral": "📱", "date": "❤️", "dates": "❤️",
             "love": "❤️", "gym": "💪", "fat": "🍔", "food": "🍔", "sleep": "😴", "dead": "💀", "kill": "💀",
             "killed": "💀", "fish": "🐟", "fishing": "🎣", "animal": "🐊", "animals": "🐊", "game": "🎮",
             "games": "🎮", "secret": "🤫", "hidden": "👀", "free": "🆓", "fast": "⚡", "speed": "⚡", "win": "🏆",
             "best": "🏆", "time": "⏰", "computer": "💻", "pc": "💻", "ai": "🤖", "robot": "🤖", "laugh": "😂",
             "funny": "😂", "heist": "💎", "steal": "🦹", "stole": "🦹", "stolen": "🦹", "boat": "🚤", "water": "🌊"}


def hook_from(sentence):
    words = re.findall(r"[A-Za-z0-9']+", sentence)
    return " ".join(words[:6]).upper()


def keywords_from(text, n=8):
    toks = re.findall(r"[A-Za-z][A-Za-z0-9']{3,}", text)
    seen, out = set(), []
    for t in sorted(toks, key=lambda t: (-len(t), toks.index(t))):
        k = t.lower()
        if k in STOPWORDS or k in seen:
            continue
        seen.add(k)
        out.append(k)
        if len(out) >= n:
            break
    out += [m for m in re.findall(r"\d[\d,.%]*", text)][:3]
    return out


def emojis_from(text, n=4):
    out, seen = [], set()
    for t in re.findall(r"[A-Za-z]+", text.lower()):
        if t in EMOJI_MAP and EMOJI_MAP[t] not in seen:
            seen.add(EMOJI_MAP[t])
            out.append({"word": t, "emoji": EMOJI_MAP[t]})
            if len(out) >= n:
                break
    return out


def ai_moments(sents, n, lo, hi, log, title="", cands=None):
    if cands:   # stream mode: only the hype candidates (keeps multi-hour VODs within one prompt)
        blocks = []
        for k, c in enumerate(cands):
            a, b = max(0, c["first"] - 2), min(len(sents) - 1, c["last"] + 2)
            body = "\n".join(f"[{i}] ({sents[i]['s']:.1f}-{sents[i]['e']:.1f}s) {sents[i]['text']}" for i in range(a, b + 1))
            blocks.append(f"### CANDIDATE {k + 1} — hype {c['hype']} ({', '.join(c['signals'])}); "
                          f"peak at {c['peak']}s; suggested span [{c['first']}..{c['last']}]\n{body}")
        lines = "\n\n".join(blocks)
        intro = ("This is a LIVE STREAM VOD. Below are hype CANDIDATES found from audio spikes, chat bursts and the "
                 "streamer's reactions. Each lists numbered sentences (with a little context either side).\n"
                 "Streams are about reactions: favour funny moments, rage, clutch plays, shocking reveals and chat "
                 "going wild — the build-up must be included so a stranger understands the payoff.\n"
                 "Use the global sentence numbers shown. You may widen or narrow a candidate's span.")
    else:
        lines = "\n".join(f"[{k}] ({s['s']:.1f}-{s['e']:.1f}s) {s['text']}" for k, s in enumerate(sents))
        intro = "Below is the full transcript split into numbered sentences with timestamps."
    prompt = f"""You are a top short-form video editor (TikTok / YouTube Shorts / Reels).
Video title: {title or 'unknown'}
{intro}

Pick the {n} BEST moments to post as standalone vertical clips. Rules:
- Each clip is a contiguous span of sentences [first..last], total length between {lo} and {hi} seconds (last.end - first.start).
- It must make sense with zero context: open on a strong hook (a surprising claim, question, or vivid detail) — never open on "And", "So", "But" mid-thought.
- It must end on a complete, satisfying thought (payoff/punchline), not mid-explanation.
- No overlaps. Skip intros, sponsor reads, "like and subscribe" outros.
- Prefer endings that LOOP: a last line that flows naturally back into the first line when the clip replays.
- Rank from strongest to weakest.

For each clip return:
- title: punchy Shorts title (max 60 chars, can use 1 emoji)
- hook: one short line on why it works
- score: 0-100 virality score (hook strength in first 3 s, curiosity, payoff, emotion, standalone clarity)
- reasons: 2-4 very short strings explaining the score (include one weakness if any)
- hook_text: on-screen hook title shown in the first 2.5 s, max 6 words, ALL CAPS, curiosity-driven, no emoji
- keywords: 4-10 single words FROM THE CLIP's own transcript that deserve visual emphasis (names, numbers, punchlines)
- emojis: 2-5 objects {{"word": "<a word that appears in the clip>", "emoji": "<one emoji>"}} for concrete nouns/feelings
- loop: true if the ending flows back into the opening

Return ONLY a JSON array, no prose, no code fences:
[{{"first": 12, "last": 19, "title": "...", "hook": "...", "score": 82, "reasons": ["..."], "hook_text": "...",
  "keywords": ["..."], "emojis": [{{"word": "car", "emoji": "🚗"}}], "loop": false}}]

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
        text = " ".join(s["text"] for s in sents[i:j + 1])
        emo = [e for e in (x.get("emojis") or []) if isinstance(e, dict) and e.get("word") and e.get("emoji")]
        out.append({"first": i, "last": j, "title": str(x.get("title", ""))[:80], "hook": str(x.get("hook", "")),
                    "score": max(0, min(100, int(x.get("score", 60) or 60))),
                    "reasons": [str(r)[:80] for r in (x.get("reasons") or [])][:4],
                    "hook_text": str(x.get("hook_text") or hook_from(sents[i]["text"]))[:48].upper(),
                    "keywords": [str(k).lower().strip(".,!?") for k in (x.get("keywords") or keywords_from(text))][:12],
                    "emojis": [{"word": str(e["word"]).lower(), "emoji": str(e["emoji"])[:8]} for e in emo][:6]
                              or emojis_from(text),
                    "loop": bool(x.get("loop"))})
    if not out:
        raise PipelineError("Claude returned no usable moments")
    return out


def stream_fallback(sents, cands, total):
    """No AI: rank hype candidates by their hype score."""
    top = max((c["hype"] for c in cands), default=1) or 1
    out = []
    for c in sorted(cands, key=lambda c: -c["hype"])[:total]:
        text = " ".join(s["text"] for s in sents[c["first"]:c["last"] + 1])
        out.append({"first": c["first"], "last": c["last"], "title": hook_from(text).title()[:60],
                    "hook": "Hype moment: " + ", ".join(c["signals"]),
                    "score": int(50 + 45 * c["hype"] / top), "reasons": c["signals"],
                    "hook_text": hook_from(sents[c["first"]]["text"]), "keywords": keywords_from(text),
                    "emojis": emojis_from(text), "loop": False})
    return sorted(out, key=lambda p: p["first"])


def pick_moments(words, n, lo, hi, use_ai, log, title="", cands=None):
    sents = sentences(words)
    total = n + 3          # a few spares the user can swap in
    picks = None
    if cands is not None and not cands:
        log("No clear hype peaks found — falling back to the transcript picker.")
        cands = None
    if use_ai:
        try:
            picks = ai_moments(sents, total, lo, hi, log, title, cands=cands)
        except Exception as e:  # fall back, never fail the job here
            log(f"AI picker unavailable ({str(e)[:120]}) — using the built-in scorer.")
    if not picks:
        log("Scoring moments with the built-in picker…")
        picks = stream_fallback(sents, cands, total) if cands else heuristic_moments(sents, total, lo, hi)
    if cands:   # surface the hype signals that led to each pick
        for p in picks:
            hit = next((c for c in cands if not (p["last"] < c["first"] or p["first"] > c["last"])), None)
            if hit:
                p["reasons"] = list(dict.fromkeys(["⚡ " + s for s in hit["signals"]] + (p.get("reasons") or [])))[:5]
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
            "score": p.get("score", 60), "reasons": p.get("reasons", []), "hook_text": p.get("hook_text", ""),
            "keywords": p.get("keywords", []), "emojis": p.get("emojis", []), "loop": p.get("loop", False),
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
