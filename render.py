"""Render one vertical clip per ffmpeg call (low-RAM safe)."""
import random
import subprocess
import threading
from pathlib import Path

import config as C
import captions
import effects as FX
import facecam
from pipeline import PipelineError, probe

W, H = 1080, 1920
RENDER_LOCK = threading.Lock()      # one heavy ffmpeg render at a time (live + queue share it)


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


def music_files():
    return sorted([p for p in C.MUSIC.iterdir() if p.suffix.lower() in C.AUDIO_EXT], key=lambda p: p.name.lower())


def pick_music(choice, index, seed):
    files = music_files()
    if not files or not choice or choice == "off":
        return None
    if choice == "random":
        return random.Random(seed * 13 + index).choice(files)
    f = C.MUSIC / choice
    return f if f.exists() else None


def graph(layout, info, gp_h, ass_rel, dur, has_audio, avoid_burned, fonts_rel="fonts", boxes=None,
          sel=None, zexpr=None, emoji_ins=(), logo_in=None, progress=None, music_in=None, music_vol=0.14,
          sfx_in=None):
    """Video+audio filter graph. Inputs: 0 = source, 1 = gameplay (split only), then extras by index."""
    sw, sh = info["width"], info["height"]
    subs = f"subtitles={ass_rel}:fontsdir={fonts_rel}"
    sharpen = ",unsharp=5:5:0.8" if gp_h and gp_h < 720 else ""
    cut = f",select='{sel}',setpts=N/FRAME_RATE/TB" if sel else ""
    src = f"[0:v]fps=30{cut}"

    def zoom(w, h):
        return ("," + FX.zoompan(zexpr, w, h)) if zexpr else ""

    if layout == "split":
        cw, ch, x, y = top_crop(sw, sh, 1080 / 960, avoid_burned)
        v = (f"{src},crop={cw}:{ch}:{x}:{y},scale=1080:960:flags=lanczos{zoom(1080, 960)},setsar=1[top];"
             f"[1:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:960{sharpen},"
             f"fps=30,setsar=1[bot];"
             f"[top][bot]vstack=inputs=2[base]")
    elif layout == "stream":
        b = boxes or {}
        face = b.get("face_box") or facecam.fit_box(sw * 0.84, sh * 0.2, sh * 0.36, sw, sh)
        game = b.get("game_box") or facecam.default_game_box(sw, sh, face)
        fw, fh, fx, fy = facecam.px_box(face, sw, sh)
        gw, gh, gx, gy = facecam.px_box(game, sw, sh)
        v = (f"{src},split=2[f][g];"
             f"[f]crop={fw}:{fh}:{fx}:{fy},scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop=1080:960{zoom(1080, 960)},setsar=1[top];"
             f"[g]crop={gw}:{gh}:{gx}:{gy},scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop=1080:960,setsar=1[bot];"
             f"[top][bot]vstack=inputs=2[base]")
    elif layout == "full":
        cw, ch, x, y = top_crop(sw, sh, 1080 / 1920, avoid_burned)
        v = f"{src},crop={cw}:{ch}:{x}:{y},scale=1080:1920:flags=lanczos{zoom(1080, 1920)},setsar=1[base]"
    else:  # fit: whole frame on a blurred fill
        ch = even(sh * 0.74) if avoid_burned else sh
        fg_h = even(1080 * ch / sw)
        v = (f"{src},crop={sw}:{ch}:0:0,split=2[a][b];"
             f"[a]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,boxblur=18:2,eq=brightness=-0.12,"
             f"scale=1080:1920,setsar=1[bg];"
             f"[b]scale=1080:{fg_h}:flags=lanczos{zoom(1080, fg_h)},setsar=1[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    last = "base"
    for k, (idx, t_a, t_b, y) in enumerate(emoji_ins):       # emoji pops with a little drop-in bounce
        v += (f";[{last}][{idx}:v]overlay=x=(W-w)/2:y='{y}-34*max(0,1-(t-{t_a:.3f})/0.18)'"
              f":enable='between(t,{t_a:.3f},{t_b:.3f})'[e{k}]")
        last = f"e{k}"
    v += f";[{last}]{subs}[s0]"
    last = "s0"
    if logo_in is not None:
        v += (f";[{logo_in}:v]scale=150:-1,format=rgba,colorchannelmixer=aa=0.85[lg];"
              f"[{last}][lg]overlay=W-w-36:36[l0]")
        last = "l0"
    if progress:
        v += (f";[{last}]drawbox=x=0:y=1906:w=iw:h=14:color=black@0.45:t=fill,"
              f"drawbox=x=0:y=1906:w='iw*t/{dur:.3f}':h=14:color={progress}@0.95:t=fill[p0]")
        last = "p0"
    v += f";[{last}]format=yuv420p[v]"
    if not has_audio:
        return v
    acut = f",aselect='{sel}',asetpts=N/SR/TB" if sel else ""
    a = (f";[0:a]aformat=sample_rates=48000:channel_layouts=stereo{acut},"
         f"afade=t=in:d=0.05,afade=t=out:st={max(dur - 0.25, 0):.2f}:d=0.25[voice]")
    mix = ["[voice]"]
    if music_in is not None:   # music ducks under the voice
        a += (f";[voice]asplit=2[vo][sc];[{music_in}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
              f"volume={music_vol:.2f},afade=t=in:d=0.6,afade=t=out:st={max(dur - 1.2, 0):.2f}:d=1.2[mus];"
              f"[mus][sc]sidechaincompress=threshold=0.025:ratio=10:attack=15:release=350[duck]")
        mix = ["[vo]", "[duck]"]
    if sfx_in is not None:
        a += f";[{sfx_in}:a]aformat=sample_rates=48000:channel_layouts=stereo[fx]"
        mix.append("[fx]")
    if len(mix) > 1:
        a += f";{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,"
    else:
        a += f";{mix[0]}"
    a += "loudnorm=I=-14:TP=-1.5:LRA=11[a]"
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
    pol = FX.polish_of(settings)
    pos_y = default_pos(layout, settings.get("position", "auto"))
    start, end = float(clip["start"]), float(clip["end"])
    ws = words_in(words, start, end)
    if not ws:
        raise PipelineError(f"No speech found in {start:.1f}-{end:.1f}s")
    t0 = max(ws[0]["s"] - 0.12, 0)
    span = ws[-1]["e"] + 0.35 - t0

    # 1) timeline: drop real silences + filler words, remap word times onto the output clock
    sil = (FX.detect_silences(src, t0, span, float(pol["max_gap"]))
           if pol["cut_silence"] and info["has_audio"] else [])
    segs, kept = FX.keep_segments(ws, pol, sil)
    segs = [(max(a, t0), min(b, t0 + span)) for a, b in segs]
    dur = sum(b - a for a, b in segs)
    ow = FX.out_words(kept, segs)
    sel = FX.select_expr(segs, t0) if (len(segs) > 1 or abs(segs[0][0] - t0) > 0.01) else None

    # 2) what to emphasise
    keywords = clip.get("keywords") or []
    hero = keywords if pol["hero"] else []
    zexpr, punches = FX.zoom_plan(ow, segs, keywords, pol, dur)
    emos = FX.emoji_events(ow, clip.get("emojis") or [], dur) if pol["emojis"] else []

    # 3) captions, all in output time
    tag = clip["id"] + ("_still" if still_at is not None else "")
    ass = job_dir / f"{tag}.ass"
    hook = (clip.get("hook_text") or clip.get("title", ""), float(pol["hook_secs"])) if pol["hook_title"] else None
    cta = (pol["cta"], max(dur - 1.9, 0), dur) if pol["end_card"] and dur > 8 else None
    top_y = 150 if layout in ("split", "stream", "full") else 260
    captions.build(ow, style, 0.0, pos_y, ass, hero=hero, hook=hook, handle=pol.get("handle", ""),
                   cta=cta, top_y=top_y)

    # 4) inputs
    inputs = [*C.FF_THREADS, "-ss", f"{t0:.3f}", "-t", f"{span:.3f}", "-i", str(src)]
    n_in = 1
    gp_h = None
    if layout == "split":
        gfile, goff, gp_h = pick_gameplay(settings.get("gameplay", "mix"), index, dur, settings.get("seed", 7))
        inputs += ["-ss", f"{goff:.2f}", "-t", f"{dur:.3f}", "-i", str(gfile)]
        n_in += 1
    emoji_ins = []
    edir = C.DATA / "emoji"
    edir.mkdir(exist_ok=True)
    for e, a, b in emos:
        png = FX.emoji_png(e, edir / ("e_" + "_".join(f"{ord(ch):x}" for ch in e) + ".png"))
        if png:
            inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
            emoji_ins.append((n_in, a, b, pos_y - 290))
            n_in += 1
    logo_in = None
    logo = C.BRAND / "clipforge-logo.png"
    if pol.get("logo") and logo.exists():
        inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(logo)]
        logo_in, n_in = n_in, n_in + 1
    has_audio = info["has_audio"] and still_at is None
    music_in = sfx_in = None
    if has_audio:
        mf = pick_music(pol.get("music"), index, settings.get("seed", 7))
        if mf:
            inputs += ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", str(mf)]
            music_in, n_in = n_in, n_in + 1
        if pol["sfx"]:
            ev = [("boom", 0.02)] if hook else []
            ev += [("whoosh", max(t - 0.12, 0)) for t in punches]
            ev += [("pop", a) for _, a, _ in emos]
            if cta:
                ev.append(("ding", cta[1]))
            sfx = FX.sfx_track(ev, dur, float(pol["sfx_vol"]), job_dir / f"{tag}_sfx.wav")
            if sfx:
                inputs += ["-i", str(sfx)]
                sfx_in, n_in = n_in, n_in + 1

    hl = (style.get("highlight") or "#FFE600").lstrip("#")
    rel = lambda p: p.relative_to(C.APP).as_posix()
    g = graph(layout, info, gp_h, rel(ass), dur, has_audio, avoid,
              boxes={k: settings.get(k) for k in ("face_box", "game_box")}, sel=sel, zexpr=zexpr,
              emoji_ins=emoji_ins, logo_in=logo_in, progress=("0x" + hl) if pol["progress"] else None,
              music_in=music_in, music_vol=float(pol["music_vol"]), sfx_in=sfx_in)
    gf = job_dir / f"{tag}.graph"
    gf.write_text(g, encoding="utf-8")
    cmd = [C.FFMPEG, "-v", "error", "-y", *inputs, "-/filter_complex", rel(gf), "-map", "[v]"]
    if still_at is not None:
        cmd += ["-ss", f"{min(float(still_at), max(dur - 0.1, 0)):.3f}", "-frames:v", "1", "-q:v", "3",
                str(out_path)]
    else:
        if has_audio:
            cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        cmd += ["-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
                "-movflags", "+faststart", str(out_path)]
    with RENDER_LOCK:
        p = subprocess.run(cmd, cwd=C.APP, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (job_dir / f"{tag}_sfx.wav").unlink(missing_ok=True)
    if p.returncode != 0:
        raise PipelineError("ffmpeg failed: " + p.stderr[-800:])
    if still_at is None:
        subprocess.run([C.FFMPEG, "-v", "error", "-y", "-ss", "1.5", "-i", str(out_path), "-frames:v", "1",
                        "-vf", "scale=360:-2", "-q:v", "4", str(out_path.with_suffix(".jpg"))],
                       capture_output=True)
    return round(dur, 2)
