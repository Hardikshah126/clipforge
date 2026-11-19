"""Stream hype detection: find the moments chat and the streamer lost it.

Signals, all on a 1-second grid:
  loud   — audio louder than the streamer's own recent baseline (screams, hype, explosions)
  chat   — chat messages per second vs. the stream's normal rate (YouTube live_chat / Twitch rechat)
  react  — reaction phrases ("no way", "let's go", "clip that"…) and [laughter]-type tokens from whisper
The combined curve's peaks become candidate clips: build-up before the peak, payoff just after it.
"""
import json
import re
from pathlib import Path

import numpy as np

REACT = re.compile(
    r"\b(no way|oh my god|oh my gosh|let'?s go+|what the|holy|insane|crazy|clip (it|that)|bro+|dude|"
    r"are you (kidding|serious)|oh no|no no no|wait wait|yo+|let'?s goo+|unbelievable|i can'?t believe|"
    r"what just happened|did you see|get him|run|help)\b", re.I)
LAUGH = re.compile(r"\[(laugh|laughter|laughing|cheer|cheering|scream|screaming|applause|gasp)", re.I)


def _z(x):
    x = np.asarray(x, dtype=np.float32)
    if not len(x):
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) + 1e-6
    return np.clip((x - med) / (1.4826 * mad), -3, 8)


def _smooth(x, k):
    if k <= 1 or len(x) < k:
        return x
    return np.convolve(x, np.ones(k) / k, mode="same")


def loud_signal(loud, n):
    """Loudness relative to a rolling 60 s baseline (so a loud streamer isn't 'always hype')."""
    step = loud.get("step", 0.5)
    db = np.asarray(loud.get("db", []), dtype=np.float32)
    if not len(db):
        return np.zeros(n)
    per = max(1, int(round(1 / step)))
    sec = db[: len(db) // per * per].reshape(-1, per).max(axis=1)        # peak dB per second
    sec = np.maximum(sec, -60)
    base = np.array([np.median(sec[max(0, i - 60): i + 1]) for i in range(len(sec))])
    out = np.zeros(n)
    m = min(n, len(sec))
    out[:m] = _z(sec[:m] - base[:m])
    return out


def chat_signal(src_dir: Path, n):
    """Messages per second from a yt-dlp chat replay (YouTube live_chat.json or Twitch rechat)."""
    files = list(src_dir.glob("*.live_chat.json")) + list(src_dir.glob("*rechat*.json"))
    if not files:
        return None, 0
    counts = np.zeros(n)
    total = 0
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        offs = [int(x) / 1000 for x in re.findall(r'"videoOffsetTimeMsec"\s*:\s*"?(\d+)', text)]
        offs += [float(x) for x in re.findall(r'"content_offset_seconds"\s*:\s*([\d.]+)', text)]
        for t in offs:
            if 0 <= t < n:
                counts[int(t)] += 1
        total += len(offs)
    if total < 30:
        return None, total
    return _z(_smooth(counts, 5)), total


def react_signal(words, raw_json: Path, n):
    out = np.zeros(n)
    # reaction phrases, matched on a rolling 4-word window
    for i in range(len(words)):
        phrase = " ".join(w["w"] for w in words[i:i + 4]).lower()
        m = REACT.match(re.sub(r"[^a-z' ]", "", phrase))
        if m:
            t = int(words[i]["s"])
            if t < n:
                out[t] += 1.5
    # whisper's bracket tokens: [laughter], [screaming]…
    try:
        raw = json.loads(raw_json.read_text(encoding="utf-8"))["transcription"]
        for x in raw:
            if LAUGH.search(x["text"]):
                t = int(x["offsets"]["from"] / 1000)
                if t < n:
                    out[t] += 2.5
    except Exception:
        pass
    return _smooth(out, 3)


def hype_curve(words, loud, src_dir: Path, duration):
    n = int(duration) + 1
    L = loud_signal(loud, n)
    Ch, chat_total = chat_signal(src_dir, n)
    R = react_signal(words, src_dir / "words.json", n)
    score = 1.0 * _smooth(L, 3) + 0.9 * R
    if Ch is not None:
        score += 1.3 * Ch
    return _smooth(score, 4), {"loud": L, "chat": Ch, "react": R, "chat_total": chat_total}


def peaks(curve, k, min_gap=75, skip_start=60, skip_end=60):
    order = np.argsort(-curve)
    picked = []
    for t in order:
        if t < skip_start or t > len(curve) - skip_end:
            continue
        if curve[t] <= 0.5:
            break
        if all(abs(t - p) >= min_gap for p in picked):
            picked.append(int(t))
        if len(picked) >= k:
            break
    return picked


def candidates(words, sents, loud, src_dir: Path, duration, n_want, lo, hi):
    """Candidate clips around hype peaks, snapped to sentence boundaries.
    Returns [{"first","last","peak","hype","signals":[...]}] (sentence indices)."""
    curve, parts = hype_curve(words, loud, src_dir, duration)
    if len(curve) < 120:
        return [], parts
    pk = peaks(curve, max(n_want * 4, 16))
    starts = np.array([s["s"] for s in sents])
    ends = np.array([s["e"] for s in sents])
    out, used = [], []
    target = (lo + hi) / 2
    for t in pk:
        a_t = t - (target - 7)                       # build-up before, ~7 s of payoff after the peak
        b_t = t + 7
        i = int(max(0, np.searchsorted(starts, a_t, side="right") - 1))
        j = int(min(len(sents) - 1, np.searchsorted(ends, b_t, side="left")))
        while j > i and sents[j]["e"] - sents[i]["s"] > hi:
            if t - sents[i]["s"] > sents[j]["e"] - t:
                i += 1
            else:
                j -= 1
        while sents[j]["e"] - sents[i]["s"] < lo and j + 1 < len(sents) and sents[j + 1]["e"] - sents[i]["s"] <= hi + 3:
            j += 1
        if any(not (j < u or i > v) for u, v in used):
            continue
        used.append((i, j))
        sig = []
        if parts["loud"][t] > 2:
            sig.append(f"audio spike ×{parts['loud'][t]:.1f}")
        if parts["chat"] is not None and parts["chat"][t] > 1.5:
            sig.append(f"chat burst ×{parts['chat'][t]:.1f}")
        if parts["react"][max(0, t - 8): t + 8].sum() > 1:
            sig.append("streamer reaction")
        out.append({"first": i, "last": j, "peak": t, "hype": round(float(curve[t]), 2),
                    "signals": sig or ["energy peak"]})
    return out, parts
