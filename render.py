"""Render one vertical clip per ffmpeg call (low-RAM safe)."""
import random
import subprocess
from pathlib import Path

import config as C
import captions
import facecam
from pipeline import PipelineError, probe

W, H = 1080, 1920


def even(x):
    return max(2, int(x) // 2 * 2)


def top_crop(sw, sh, aspect, avoid_burned):
    """Source crop (w,h,x,y) with the given w/h aspect; optionally skip the bottom 26% (burned-in subs)."""
    usable_h = sh * 0.74 if avoid_burned else sh
    if sw / usable_h > aspect:
        ch = usable_h
        cw = ch * aspect
    else:
        cw = sw
        ch = cw / aspect
    cw, ch = even(min(cw, sw)), even(min(ch, usable_h))
    x = even((sw - cw) / 2)
    y = 0 if avoid_burned else even((sh - ch) / 2)
    return cw, ch, x, y


def gameplay_files():
    return sorted([p for p in C.GAMEPLAY.iterdir() if p.suffix.lower() in C.VIDEO_EXT], key=lambda p: p.name.lower())


def pick_gameplay(choice, index, dur, seed):
    files = gameplay_files()
    if not files:
        raise PipelineError("No gameplay videos found — add one in the Gameplay library.")
    if choice and choice not in ("mix", "random"):
        f = C.GAMEPLAY / choice
        if not f.exists():
            raise PipelineError(f"Gameplay file not found: {choice}")
    elif choice == "random":
        f = random.Random(seed + index).choice(files)
    else:
        f = files[index % len(files)]
    gd = probe(f)["duration"]
    span = max(gd - dur - 2, 0)
    off = (random.Random(seed * 31 + index).uniform(0.05, 0.95) * span) if span else 0
    return f, round(off, 2), probe(f)["height"] or 1080


def graph(layout, info, gp_h, ass_rel, dur, has_audio, avoid_burned, fonts_rel="fonts", boxes=None):
    sw, sh = info["width"], info["height"]
    subs = f"subtitles={ass_rel}:fontsdir={fonts_rel}"
    sharpen = ",unsharp=5:5:0.8" if gp_h and gp_h < 720 else ""
    if layout == "split":
        cw, ch, x, y = top_crop(sw, sh, 1080 / 960, avoid_burned)
        v = (f"[0:v]crop={cw}:{ch}:{x}:{y},scale=1080:960:flags=lanczos,fps=30,setsar=1[top];"
             f"[1:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960{sharpen},fps=30,setsar=1[bot];"
             f"[top][bot]vstack=inputs=2,{subs},format=yuv420p[v]")
    elif layout == "stream":
        b = boxes or {}
        face = b.get("face_box") or facecam.fit_box(sw * 0.84, sh * 0.2, sh * 0.36, sw, sh)
        game = b.get("game_box") or facecam.default_game_box(sw, sh, face)
        fw, fh, fx, fy = facecam.px_box(face, sw, sh)
        gw, gh, gx, gy = facecam.px_box(game, sw, sh)
        v = (f"[0:v]fps=30,split=2[f][g];"
             f"[f]crop={fw}:{fh}:{fx}:{fy},scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960,setsar=1[top];"
             f"[g]crop={gw}:{gh}:{gx}:{gy},scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960,setsar=1[bot];"
             f"[top][bot]vstack=inputs=2,{subs},format=yuv420p[v]")
    elif layout == "full":
        cw, ch, x, y = top_crop(sw, sh, 1080 / 1920, avoid_burned)
        v = f"[0:v]crop={cw}:{ch}:{x}:{y},scale=1080:1920:flags=lanczos,fps=30,setsar=1,{subs},format=yuv420p[v]"
    else:  # fit: whole frame on a blurred fill
        ch = even(sh * 0.74) if avoid_burned else sh
        v = (f"[0:v]crop={sw}:{ch}:0:0,fps=30,split=2[a][b];"
             f"[a]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,boxblur=18:2,eq=brightness=-0.12,"
             f"scale=1080:1920,setsar=1[bg];"
             f"[b]scale=1080:-2:flags=lanczos,setsar=1[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{subs},format=yuv420p[v]")
    a = f";[0:a]afade=t=in:d=0.06,afade=t=out:st={max(dur - 0.25, 0):.2f}:d=0.25,loudnorm=I=-14:TP=-1.5:LRA=11[a]" if has_audio else ""
    return v + a


def default_pos(layout, position):
    if position and position != "auto":
        return captions.POSITIONS.get(position, 960)
    return {"split": 960, "stream": 960, "full": 1380, "fit": 1460}.get(layout, 960)


def words_in(words, start, end):
    return [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.1]


def render_clip(job_dir: Path, src: Path, info, words, clip, settings, index, out_path: Path, still_at=None):
    layout = settings.get("layout", "split")
    style = settings.get("style", {})
    avoid = bool(settings.get("avoid_burned", True))
    pos_y = default_pos(layout, settings.get("position", "auto"))
    start, end = float(clip["start"]), float(clip["end"])
    ws = words_in(words, start, end)
    if not ws:
        raise PipelineError(f"No speech found in {start:.1f}-{end:.1f}s")
    t0 = max(ws[0]["s"] - 0.12, 0)
    t1 = ws[-1]["e"] + 0.35
    dur = t1 - t0
    seek = t0 + (still_at or 0)
    tag = clip["id"] + ("_still" if still_at is not None else "")
    ass = job_dir / f"{tag}.ass"
    captions.build(ws, style, seek, pos_y, ass)

    inputs = [*C.FF_THREADS, "-ss", f"{seek:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    gp_h = None
    if layout == "split":
        gfile, goff, gp_h = pick_gameplay(settings.get("gameplay", "mix"), index, dur, settings.get("seed", 7))
        inputs += ["-ss", f"{goff + (still_at or 0):.2f}", "-t", f"{dur:.3f}", "-i", str(gfile)]
    rel = lambda p: p.relative_to(C.APP).as_posix()
    has_audio = info["has_audio"] and still_at is None
    g = graph(layout, info, gp_h, rel(ass), dur, has_audio, avoid,
              boxes={k: settings.get(k) for k in ("face_box", "game_box")})
    gf = job_dir / f"{tag}.graph"
    gf.write_text(g, encoding="utf-8")
    cmd = [C.FFMPEG, "-v", "error", "-y", *inputs, "-/filter_complex", rel(gf), "-map", "[v]"]
    if still_at is not None:
        cmd += ["-frames:v", "1", "-q:v", "3", str(out_path)]
    else:
        if has_audio:
            cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
                "-movflags", "+faststart", str(out_path)]
    p = subprocess.run(cmd, cwd=C.APP, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise PipelineError("ffmpeg failed: " + p.stderr[-800:])
    if still_at is None:
        subprocess.run([C.FFMPEG, "-v", "error", "-y", "-ss", "1.5", "-i", str(out_path), "-frames:v", "1",
                        "-vf", "scale=360:-2", "-q:v", "4", str(out_path.with_suffix(".jpg"))],
                       capture_output=True)
    return round(dur, 2)
