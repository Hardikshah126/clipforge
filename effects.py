"""Polish layer: jump cuts, punch-in zooms, sound effects, emoji pops.

All times here are in OUTPUT seconds (after silence/filler cuts) unless named src_*.
"""
import re
import subprocess
from pathlib import Path

import config as C

FILLERS = {"um", "uh", "uhm", "umm", "erm", "er", "ah", "hmm", "mm", "uh-huh"}
SFX_DIR = C.APP / "sfx"

DEFAULT_POLISH = dict(
    cut_silence=True, max_gap=0.35, cut_fillers=True,
    zoom="subtle",               # off | subtle | punchy
    hero=True, emojis=True, hook_title=True, hook_secs=2.6, cold_open=True,
    sfx=True, sfx_vol=0.55, music="off", music_vol=0.14,
    progress=True, handle="", logo=False, end_card=True, cta="FOLLOW FOR MORE",
)


def polish_of(settings):
    return {**DEFAULT_POLISH, **(settings.get("polish") or {})}


# ------------------------------------------------------------------ cuts
def _bare(w):
    return re.sub(r"[^a-z\-]", "", w.lower())


def detect_silences(src, t0, dur, min_gap, noise_db=-33):
    """Real pauses from the audio level (whisper stretches word timings over gaps, so words alone can't tell)."""
    p = subprocess.run([C.FFMPEG, "-v", "info", *C.FF_THREADS, "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                        "-vn", "-af", f"silencedetect=n={noise_db}dB:d={min_gap:.2f}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out, start = [], None
    for line in p.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            start = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            out.append((t0 + start, t0 + float(m.group(1))))
            start = None
    return out


def keep_segments(words, pol, silences=()):
    """Source-time segments to keep: drop real silences (from audio) and filler words."""
    pad = 0.07
    fill = bool(pol.get("cut_fillers"))
    ws = [w for w in words if not (fill and _bare(w["w"]) in FILLERS)] or words
    a0, b0 = ws[0]["s"] - 0.1, ws[-1]["e"] + 0.3
    cuts = []
    if pol.get("cut_silence"):
        cuts += [(s + pad, e - pad) for s, e in silences if e - s > 2 * pad + 0.1]
    if fill:
        cuts += [(w["s"], w["e"]) for w in words if _bare(w["w"]) in FILLERS]
    cuts = sorted((max(a, a0), min(b, b0)) for a, b in cuts if b > a0 and a < b0)
    segs, cur = [], a0
    for a, b in cuts:
        if a > cur:
            segs.append([cur, a])
        cur = max(cur, b)
    if cur < b0:
        segs.append([cur, b0])
    out = []
    for a, b in segs:
        if out and a - out[-1][1] < 0.05:
            out[-1][1] = b
        elif b - a > 0.08:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out], ws


def remap(t, segs):
    """Source time -> output time for a list of kept segments."""
    acc = 0.0
    for a, b in segs:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc


def out_words(ws, segs):
    res = []
    for w in ws:
        s, e = remap(w["s"], segs), remap(w["e"], segs)
        res.append({"s": s, "e": max(e, s + 0.05), "w": w["w"]})
    return res


def select_expr(segs, t0):
    return "+".join(f"between(t,{a - t0:.3f},{b - t0:.3f})" for a, b in segs)


# ------------------------------------------------------------------ hook: weak openers + cold-open tease
WEAK_OPEN = {"so", "and", "but", "okay", "ok", "yeah", "yes", "well", "like", "um", "uh", "basically", "anyway",
             "alright", "right", "now", "oh", "hmm", "see", "look", "listen"}


def strip_weak_open(words, limit=3):
    """Drop up to `limit` throat-clearing words at the very start ('So, yeah, ...') so the clip opens on content."""
    k = 0
    while k < min(limit, len(words) - 4) and _bare(words[k]["w"]) in WEAK_OPEN:
        k += 1
    return words[k:]


def _tok(s):
    return re.sub(r"[^a-z0-9']", "", s.lower())


def find_tease(words, clip, min_len=1.2, max_len=4.2):
    """Source-time (a, b) of the cold-open line, or None.
    Uses the AI's word-for-word `tease` quote, else `tease_at` (live hype peak)."""
    if len(words) < 8:
        return None
    clip_s, clip_e = words[0]["s"], words[-1]["e"]
    if clip_e - clip_s < 12:                       # too short to be worth a flash-forward
        return None
    q = [t for t in (_tok(x) for x in str(clip.get("tease") or "").split()) if t]
    span = None
    if len(q) >= 3:
        toks = [_tok(w["w"]) for w in words]
        best, bi = 0, -1
        for i in range(len(toks) - len(q) + 1):
            sc = sum(1 for k, t in enumerate(q) if toks[i + k] == t)
            if sc > best:
                best, bi = sc, i
        if bi >= 0 and best >= 0.6 * len(q):
            span = (bi, bi + len(q) - 1)
    elif clip.get("tease_at") is not None:
        t = float(clip["tease_at"])
        near = [i for i, w in enumerate(words) if w["s"] <= t + 1.5 and w["e"] >= t - 2.5]
        if near:
            span = (near[0], near[-1])
    if not span:
        return None
    i, j = span
    while j > i and words[j]["e"] - words[i]["s"] > max_len:     # keep the end (punchlines land last)
        i += 1
    while words[j]["e"] - words[i]["s"] < min_len and (i > 0 or j < len(words) - 1):
        if j < len(words) - 1:
            j += 1
        else:
            i -= 1
    a, b = words[i]["s"] - 0.05, words[j]["e"] + 0.15
    if j + 1 < len(words):                          # never bleed into the next word
        b = min(b, max(words[j + 1]["s"] - 0.01, words[j]["e"]))
    if i > 0:
        a = max(a, words[i - 1]["e"])
    if a - clip_s < 3:                              # teasing the opening line itself adds nothing
        return None
    return round(a, 3), round(b, 3)


# ------------------------------------------------------------------ zooms
def zoom_plan(words_out, segs, keywords, pol, dur):
    """Return (zoompan z-expression, list of punch times)."""
    mode = pol.get("zoom", "subtle")
    if mode == "off":
        return None, []
    step, punch = (0.06, 0.10) if mode == "subtle" else (0.10, 0.18)
    terms, punches = [], []
    # 1) alternate framing at every jump cut — hides the cut, feels edited
    acc = 0.0
    bounds = []
    for a, b in segs:
        bounds.append(acc)
        acc += b - a
    bounds.append(acc)
    for k in range(len(bounds) - 1):
        if k % 2 == 1:
            terms.append(f"{step}*between(it,{bounds[k]:.3f},{bounds[k + 1]:.3f})")
    # 2) quick eased punch-ins on hero words, at most one every 2.5 s
    kw = {k.lower() for k in keywords}
    last = -9
    for w in words_out:
        if re.sub(r"[^a-z0-9]", "", w["w"].lower()) in kw and w["s"] - last >= 2.5 and w["s"] > 0.8:
            T, E = w["s"], min(w["s"] + 0.9, dur)
            terms.append(f"{punch}*min(1,(it-{T:.3f})/0.12)*between(it,{T:.3f},{E:.3f})")
            punches.append(T)
            last = T
    if not terms:
        return None, []
    return "1+" + "+".join(terms), punches


def zoompan(zexpr, w, h):
    """Smooth zoom: upscale 2x first so zoompan's integer cropping doesn't jitter."""
    return (f"scale={w * 2}:{h * 2}:flags=bicubic,"
            f"zoompan=z='{zexpr}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps=30")


# ------------------------------------------------------------------ sound effects
def ensure_sfx():
    """Synthesize a tiny SFX kit once (no downloads, no licensing questions)."""
    SFX_DIR.mkdir(exist_ok=True)
    kit = {
        "whoosh": "anoisesrc=color=pink:d=0.42:a=0.9,highpass=f=700,lowpass=f=7000,"
                  "afade=t=in:d=0.18,afade=t=out:st=0.2:d=0.22,volume=0.9",
        "pop": "aevalsrc='0.9*sin(2*PI*(500+1800*exp(-t*45))*t)*exp(-t*28)':d=0.16:s=48000",
        "boom": "aevalsrc='0.95*sin(2*PI*(48+90*exp(-t*14))*t)*exp(-t*4.5)':d=0.95:s=48000,lowpass=f=400,volume=1.6",
        "ding": "aevalsrc='0.5*(sin(2*PI*1320*t)+0.6*sin(2*PI*1980*t))*exp(-t*6)':d=0.7:s=48000",
    }
    for name, src in kit.items():
        f = SFX_DIR / f"{name}.wav"
        if not f.exists():
            subprocess.run([C.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src, "-ac", "2", "-ar", "48000", str(f)],
                           capture_output=True)
    return {k: SFX_DIR / f"{k}.wav" for k in kit}


def sfx_track(events, dur, vol, out: Path):
    """events: [(name, t)] -> one stereo wav of length dur. Separate tiny ffmpeg call (audio only)."""
    kit = ensure_sfx()
    events = [(n, t) for n, t in events if n in kit and 0 <= t < dur - 0.05][:24]
    if not events:
        return None
    names = sorted({n for n, _ in events})
    cmd = [C.FFMPEG, "-v", "error", "-y"]
    for n in names:
        cmd += ["-i", str(kit[n])]
    parts, labels = [], []
    for k, n in enumerate(names):
        uses = [t for m, t in events if m == n]
        outs = "".join(f"[{n}{j}]" for j in range(len(uses)))
        parts.append(f"[{k}:a]asplit={len(uses)}{outs}" if len(uses) > 1 else f"[{k}:a]anull{outs}")
        for j, t in enumerate(uses):
            ms = int(t * 1000)
            parts.append(f"[{n}{j}]adelay={ms}|{ms}[d{n}{j}]")
            labels.append(f"[d{n}{j}]")
    parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,volume={vol:.2f},"
                 f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}[o]")
    gf = out.with_suffix(".sfx.graph")
    gf.write_text(";".join(parts), encoding="utf-8")
    cmd += ["-/filter_complex", str(gf), "-map", "[o]", "-ac", "2", "-ar", "48000", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    gf.unlink(missing_ok=True)
    return out if p.returncode == 0 and out.exists() else None


# ------------------------------------------------------------------ emoji
_EMOJI_FONT = Path(r"C:\Windows\Fonts\seguiemj.ttf")


def emoji_png(emoji, out: Path, size=240):
    from PIL import Image, ImageDraw, ImageFont
    if out.exists():
        return out
    font = ImageFont.truetype(str(_EMOJI_FONT), 109)          # Segoe UI Emoji bitmap strike
    im = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((80, 80), emoji, font=font, embedded_color=True, anchor="mm")
    bbox = im.getbbox()
    if not bbox:
        return None
    im = im.crop(bbox)
    im.thumbnail((size, size), Image.LANCZOS)
    # soft drop shadow so it reads on any background
    sh = Image.new("RGBA", (im.width + 16, im.height + 16), (0, 0, 0, 0))
    a = im.split()[3].point(lambda v: int(v * 0.45))
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 255))
    shadow.putalpha(a)
    sh.alpha_composite(shadow, (10, 12))
    sh.alpha_composite(im, (4, 2))
    sh.save(out)
    return out


def emoji_events(words_out, emojis, dur, limit=4):
    """[(emoji, t_start, t_end)] — first spoken occurrence of each emoji's word, spaced out."""
    res, last = [], -9
    for e in emojis:
        word = re.sub(r"[^a-z0-9]", "", str(e.get("word", "")).lower())
        if not word:
            continue
        for w in words_out:
            if re.sub(r"[^a-z0-9]", "", w["w"].lower()) == word and w["s"] - last >= 1.6 and w["s"] < dur - 1.2:
                res.append((e["emoji"], w["s"], min(w["s"] + 1.25, dur)))
                last = w["s"]
                break
        if len(res) >= limit:
            break
    return sorted(res, key=lambda x: x[1])
