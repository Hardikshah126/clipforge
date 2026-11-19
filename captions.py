"""Word-synced ASS caption engine with style presets.

Every preset is a dict of plain values so the front-end can show and override them.
mode decides how the active (spoken) word is treated:
  pop     active word recolored + scales up with a quick pop
  box     active word sits on a solid rounded highlight block
  dim     unspoken words are faded, active word full white (clean / premium)
  reveal  words appear one by one as they're spoken, newest highlighted
  single  one big word at a time with a bounce, colors rotate
  neon    glowing outline layer under crisp text, active word lit up
"""
import re

FONTS = {  # display name -> (ASS family, bold, italic)
    "Poppins ExtraBold": ("Poppins ExtraBold", 0, 0),
    "Lilita One": ("Lilita One", 0, 0),
    "Inter Tight ExtraBold": ("Inter Tight ExtraBold", 0, 0),
    "Impact": ("Impact", 0, 0),
    "Segoe UI Black": ("Segoe UI Black", 0, 0),
    "Space Mono": ("Space Mono", 1, 0),
    "Instrument Serif": ("Instrument Serif", 0, 1),
}

PRESETS = {
    "pop": dict(label="Pop Yellow", blurb="Bold white, spoken word turns yellow and pops",
                mode="pop", font="Poppins ExtraBold", size=84, upper=True, color="#FFFFFF",
                highlight="#FFE600", outline="#000000", outline_w=7, shadow=4, max_words=3,
                max_chars=18, pop=116, strip_punct=True),
    "box": dict(label="Highlight Box", blurb="Active word sits on a solid color block",
                mode="box", font="Lilita One", size=90, upper=True, color="#FFFFFF",
                highlight="#7C3AED", outline="#000000", outline_w=6, shadow=3, max_words=3,
                max_chars=16, pop=106, strip_punct=True),
    "clean": dict(label="Clean Premium", blurb="Sentence case, unspoken words dimmed",
                  mode="dim", font="Inter Tight ExtraBold", size=70, upper=False, color="#FFFFFF",
                  highlight="#FFFFFF", outline="#000000", outline_w=0, shadow=5, max_words=4,
                  max_chars=24, pop=100, strip_punct=False),
    "reveal": dict(label="Word Reveal", blurb="Words land one at a time as they're said",
                   mode="reveal", font="Poppins ExtraBold", size=80, upper=True, color="#FFFFFF",
                   highlight="#22E06B", outline="#000000", outline_w=7, shadow=4, max_words=4,
                   max_chars=20, pop=112, strip_punct=True),
    "single": dict(label="One Word Punch", blurb="One huge word at a time, bouncing in",
                   mode="single", font="Lilita One", size=128, upper=True, color="#FFFFFF",
                   highlight="#FFE600", outline="#000000", outline_w=9, shadow=5, max_words=1,
                   max_chars=14, pop=112, strip_punct=True),
    "neon": dict(label="Neon Glow", blurb="Glowing outline, active word lights up",
                 mode="neon", font="Segoe UI Black", size=80, upper=True, color="#FFFFFF",
                 highlight="#38E1FF", outline="#0A84FF", outline_w=5, shadow=0, max_words=3,
                 max_chars=18, pop=110, strip_punct=True),
}
SINGLE_PALETTE = ["#FFE600", "#FFFFFF", "#22E06B", "#FFFFFF", "#FF4D6D", "#FFFFFF", "#38E1FF", "#FFFFFF"]

POSITIONS = {"seam": 960, "upper": 620, "center": 960, "lower": 1380}

FIX = {"i": "I", "i'm": "I'm", "i'll": "I'll", "i've": "I've", "i'd": "I'd", "gta": "GTA", "ai": "AI"}


def ass_color(hex_, alpha=0):
    h = hex_.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}&".upper().replace("&H", "&H", 1)


def ts(t):
    t = max(t, 0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean_word(w, st):
    w = w.replace("{", "").replace("}", "").replace("\\", "")
    w = FIX.get(w.lower(), w) if not st["upper"] else w
    if st["strip_punct"]:
        w = re.sub(r"[.,;:]+$", "", w)          # keep ? and ! — they carry tone
    return w.upper() if st["upper"] else w


def chunk(words, st):
    """Group words into caption lines: limits, punctuation, pauses; no orphan single words."""
    mw, mc = int(st["max_words"]), int(st["max_chars"])
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        text_len = sum(len(x["w"]) + 1 for x in cur)
        nxt_len = text_len + (len(nxt["w"]) if nxt else 0)
        brk = (len(cur) >= mw or nxt_len > mc or w["w"][-1] in ".?!,;:"
               or (nxt and nxt["s"] - w["e"] > 0.4))
        if brk or not nxt:
            out.append(cur)
            cur = []
    if mw > 1:  # merge a lone short word into its neighbour when there's room
        merged = []
        for g in out:
            if (len(g) == 1 and merged and len(merged[-1]) < mw + 1 and len(g[0]["w"]) <= 4
                    and merged[-1][-1]["w"][-1] not in ".?!" and g[0]["s"] - merged[-1][-1]["e"] < 0.3):
                merged[-1] = merged[-1] + g
            else:
                merged.append(g)
        out = merged
    return out


HERO_DEFAULT = {"pop": "#22E06B", "box": "#FFE600", "dim": "#FFE600", "reveal": "#FFE600", "single": "#22E06B",
                "neon": "#FF4DD2"}


def _bare(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def build(words, st, t0, pos_y, out_path, hero=(), hook=None, handle="", cta=None, top_y=None):
    """words: [{'s','e','w'}]; t0 is subtracted from every time.
    hero: words always drawn in the hero color. hook: (text, seconds) title at the top.
    handle: @handle watermark. cta: (text, start, end) end-card call to action (output time)."""
    st = {**PRESETS["pop"], **st}
    fam, bold, ital = FONTS.get(st["font"], FONTS["Poppins ExtraBold"])
    col, hl = ass_color(st["color"]), ass_color(st["highlight"])
    hero = {_bare(h) for h in hero if h}
    hc = ass_color(st.get("hero") or HERO_DEFAULT.get(st["mode"], "#22E06B"))
    oc = ass_color(st["outline"])
    ow, sh = float(st["outline_w"]), float(st["shadow"])
    pop = int(st["pop"])
    mode = st["mode"]
    groups = chunk(words, st)
    ev = []

    def add(layer, a, b, text):
        ra, rb = max(a - t0, 0.0), b - t0
        if rb - ra >= 0.03:      # zero-length events confuse ffmpeg's ASS decoder — drop them
            ev.append(f"Dialogue: {layer},{ts(ra)},{ts(rb)},Cap,,0,0,0,,{text}")

    for gi, g in enumerate(groups):
        nxt_start = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else None
        g_end = g[-1]["e"] + 0.45
        if nxt_start is not None:
            g_end = min(g_end, nxt_start) if nxt_start - g[-1]["e"] < 0.8 else g[-1]["e"] + 0.45
        g_end = max(g_end, g[-1]["e"] + 0.05)
        words_txt = [clean_word(w["w"], st) for w in g]
        is_hero = [_bare(w["w"]) in hero for w in g]
        for wi, w in enumerate(g):
            a = w["s"] if wi else g[0]["s"]
            b = g[wi + 1]["s"] if wi + 1 < len(g) else g_end
            enter = "\\fscx86\\fscy86\\alpha&H40&\\t(0,70,\\fscx100\\fscy100\\alpha&H00&)" if wi == 0 else ""
            base = "{\\an5\\pos(540,%d)%s}" % (pos_y, enter)
            parts = []
            for wj, txt in enumerate(words_txt):
                if is_hero[wj] and wj != wi and not (mode == "reveal" and wj > wi):
                    txt = "{\\c%s}%s{\\c%s}" % (hc, txt, col)      # hero words keep their own color
                if mode == "pop":
                    parts.append("{\\c%s\\t(0,80,\\fscx%d\\fscy%d)}%s{\\c%s\\fscx100\\fscy100}" % (hl, pop, pop, txt, col)
                                 if wj == wi else txt)
                elif mode == "box":
                    parts.append(("{\\c%s\\3c%s\\bord%d\\shad0\\fscx%d\\fscy%d}%s{\\c%s\\3c%s\\bord%s\\shad%s\\fscx100\\fscy100}"
                                  % (col, hl, int(ow * 2.6 + 6), pop, pop, txt, col, oc, ow, sh)) if wj == wi else txt)
                elif mode == "dim":
                    parts.append("{\\alpha&H00&\\c%s}%s" % (hl, txt) if wj == wi else
                                 ("{\\alpha&H00&}%s" % txt if is_hero[wj] else "{\\alpha&H70&\\c%s}%s" % (col, txt)))
                elif mode == "reveal":
                    if wj < wi:
                        parts.append("{\\alpha&H00&}%s" % txt if is_hero[wj] else "{\\alpha&H00&\\c%s}%s" % (col, txt))
                    elif wj == wi:
                        parts.append("{\\alpha&H00&\\c%s\\fscx%d\\fscy%d\\t(0,90,\\fscx100\\fscy100)}%s{\\fscx100\\fscy100}"
                                     % (hl, pop + 8, pop + 8, txt))
                    else:
                        parts.append("{\\alpha&HFF&}%s" % txt)   # reserve the space so nothing jumps
                elif mode == "neon":
                    parts.append("{\\c%s}%s{\\c%s}" % (hl, txt, col) if wj == wi else txt)
                else:
                    parts.append(txt)
            if mode == "single":
                c = hc if is_hero[wi] else (ass_color(SINGLE_PALETTE[gi % len(SINGLE_PALETTE)]) if st["highlight"] else col)
                text = "{\\an5\\pos(540,%d)\\c%s\\fscx70\\fscy70\\t(0,90,\\fscx%d\\fscy%d)\\t(90,170,\\fscx100\\fscy100)}%s" % (
                    pos_y, c, pop, pop, words_txt[wi])
                add(1, w["s"], b if wi + 1 < len(g) else g_end, text)
                continue
            if mode == "neon":
                glow = "{\\an5\\pos(540,%d)\\1a&HFF&\\3c%s\\bord%d\\blur9\\shad0}" % (pos_y, oc, int(ow + 7))
                add(0, a, b, glow + " ".join(words_txt))
            add(1, a, b, base + " ".join(parts))

    def esc(t):
        return t.replace("{", "(").replace("}", ")").replace("\\", "")

    if hook and str(hook[0]).strip():          # hook title: dark text on a highlight-colored box, pops in
        secs = float(hook[1])
        ev.append("Dialogue: 5,%s,%s,Hook,,0,0,0,,{\\an8\\pos(540,%d)\\q0\\fscx70\\fscy70\\t(0,110,\\fscx106\\fscy106)"
                  "\\t(110,200,\\fscx100\\fscy100)\\fad(0,200)}%s" % (ts(0), ts(secs), top_y or 210, esc(str(hook[0]).upper())))
    if handle and handle.strip():
        h = handle.strip() if handle.strip().startswith("@") else "@" + handle.strip()
        ev.append("Dialogue: 4,%s,%s,Handle,,0,0,0,,{\\an2\\pos(540,1872)}%s" % (ts(0), ts(9999), esc(h)))
    if cta and str(cta[0]).strip():
        a, b = float(cta[1]), float(cta[2])
        ev.append("Dialogue: 6,%s,%s,Cta,,0,0,0,,{\\an5\\pos(540,%d)\\fscx55\\fscy55\\t(0,150,\\fscx110\\fscy110)"
                  "\\t(150,260,\\fscx100\\fscy100)}%s" % (ts(a), ts(b), min(pos_y + 300, 1640), esc(str(cta[0]).upper())))
    shadow_col = "&H64000000&" if mode != "dim" else "&H90000000&"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{fam},{int(st['size'])},{col},{col},{oc},{shadow_col},{-1 if bold else 0},{-1 if ital else 0},0,0,100,100,1,0,1,{ow},{sh},5,60,60,0,1
Style: Hook,{fam},86,&H00111111&,&H00111111&,{hl},&H00000000&,{-1 if bold else 0},0,0,0,100,100,1,0,3,16,0,8,100,100,0,1
Style: Handle,{fam},40,&H30FFFFFF&,&H30FFFFFF&,&H90000000&,&H90000000&,{-1 if bold else 0},0,0,0,100,100,1,0,1,3,0,2,60,60,0,1
Style: Cta,{fam},80,{hl},{hl},&H00000000&,&H64000000&,{-1 if bold else 0},0,0,0,100,100,2,0,1,8,5,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    out_path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")
    return out_path


SAMPLE = [{"s": 0.0, "e": 0.3, "w": "This"}, {"s": 0.3, "e": 0.55, "w": "is"}, {"s": 0.55, "e": 1.0, "w": "how"},
          {"s": 1.0, "e": 1.3, "w": "your"}, {"s": 1.3, "e": 1.8, "w": "captions"}, {"s": 1.8, "e": 2.3, "w": "look!"}]
