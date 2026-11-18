"""Find a streamer's facecam in a stream VOD and propose crop boxes.

Boxes are normalized {x, y, w, h} in 0..1 of the source frame. Both halves of the
vertical output are 1080x960, so every box keeps a pixel aspect of 1080/960.
"""
import subprocess
from collections import Counter
from pathlib import Path

import config as C

ASPECT = 1080 / 960


def _frame(src, t, out: Path, width=960):
    subprocess.run([C.FFMPEG, "-v", "error", "-y", *C.FF_THREADS, "-ss", f"{t:.2f}", "-i", str(src),
                    "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(out)],
                   capture_output=True)
    return out if out.exists() else None


def fit_box(cx, cy, h, W, H):
    """Box of pixel height h centred on (cx, cy) with output aspect, clamped inside the frame (pixels)."""
    w = h * ASPECT
    if w > W:
        w, h = W, W / ASPECT
    if h > H:
        h, w = H, H * ASPECT
    x = min(max(cx - w / 2, 0), W - w)
    y = min(max(cy - h / 2, 0), H - h)
    return {"x": x / W, "y": y / H, "w": w / W, "h": h / H}


def default_game_box(W, H, face=None):
    """Gameplay crop: full height, output aspect; slide away from the facecam if it sits at a side."""
    h = H
    w = min(W, h * ASPECT)
    x = (W - w) / 2
    if face:
        fx0, fx1 = face["x"] * W, (face["x"] + face["w"]) * W
        if fx1 < W * 0.4:            # facecam on the left -> lean right
            x = min(W - w, max(x, fx1))
        elif fx0 > W * 0.6:          # facecam on the right -> lean left
            x = max(0, min(x, fx0 - w))
    return {"x": x / W, "y": 0.0, "w": w / W, "h": h / H}


YUNET = C.APP / "models" / "face_detection_yunet_2023mar.onnx"


def _detector(log):
    """Return f(img_bgr) -> [(x, y, w, h)], or None when no face detector is available."""
    try:
        import cv2
    except Exception:
        log("OpenCV isn't installed — automatic face detection is off.")
        return None
    if YUNET.exists() and hasattr(cv2, "FaceDetectorYN"):
        det = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.75, 0.3, 50)

        def run(img):
            h, w = img.shape[:2]
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            return [] if faces is None else [tuple(int(v) for v in f[:4]) for f in faces]
        return run
    if hasattr(cv2, "CascadeClassifier"):
        xml = Path(getattr(getattr(cv2, "data", None), "haarcascades", "")) / "haarcascade_frontalface_default.xml"
        if xml.exists():
            cascade = cv2.CascadeClassifier(str(xml))

            def run(img):
                gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
                W = img.shape[1]
                return list(cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                                     minSize=(int(W * 0.035),) * 2))
            return run
    log(f"No face-detection model found (expected {YUNET.name} in clipforge/models) — automatic detection is off.")
    return None


def detect(src: Path, duration: float, work: Path, log=print):
    import cv2  # imported lazily so the app still runs without OpenCV
    work.mkdir(parents=True, exist_ok=True)
    find_faces = _detector(log)
    n = 14
    times = [duration * (0.06 + 0.88 * i / (n - 1)) for i in range(n)]
    hits, frames = [], []
    for i, t in enumerate(times):
        f = _frame(src, t, work / f"probe_{i:02d}.jpg")
        if not f:
            continue
        img = cv2.imread(str(f))
        if img is None:
            continue
        H, W = img.shape[:2]
        faces = find_faces(img) if find_faces else []
        frames.append((f, W, H))
        for (x, y, w, h) in faces:
            hits.append({"i": i, "cx": (x + w / 2) / W, "cy": (y + h / 2) / H, "fw": w / W, "fh": h / H})
    if not frames:
        raise RuntimeError("Could not read frames from the video.")
    W, H = frames[0][1], frames[0][2]

    # the facecam is the face that sits in the SAME place across many frames
    bins = Counter()
    for h in hits:
        bins[(round(h["cx"] * 8), round(h["cy"] * 8))] += 1
    face = None
    best_frame = frames[len(frames) // 2][0]
    found = False
    if bins:
        (bx, by), count = bins.most_common(1)[0]
        frames_with = {h["i"] for h in hits if (round(h["cx"] * 8), round(h["cy"] * 8)) == (bx, by)}
        if len(frames_with) >= max(3, len(frames) // 4):
            sel = sorted([h for h in hits if (round(h["cx"] * 8), round(h["cy"] * 8)) == (bx, by)],
                         key=lambda h: h["fw"])
            m = sel[len(sel) // 2]
            fh_px = m["fh"] * H
            # head + shoulders: ~3.1 face heights tall, face sits a little above centre
            face = fit_box(m["cx"] * W, m["cy"] * H + fh_px * 0.45, fh_px * 3.1, W, H)
            best_frame = frames[min(frames_with)][0]
            found = True
    if not face:
        log("No steady facecam found — placed a default box top-right; drag it onto the face.")
        face = fit_box(W * 0.84, H * 0.2, H * 0.36, W, H)
    else:
        log("Facecam found — framing head & shoulders.")
    game = default_game_box(W, H, face)
    keep = work / "frame.jpg"
    if best_frame != keep:
        Path(best_frame).replace(keep)
    for p in work.glob("probe_*.jpg"):
        p.unlink(missing_ok=True)
    return {"face_box": face, "game_box": game, "found": found, "frame": keep}


def px_box(box, sw, sh):
    """Normalized box -> even pixel crop (w, h, x, y) for ffmpeg."""
    ev = lambda v: max(2, int(round(v)) // 2 * 2)
    w, h = ev(box["w"] * sw), ev(box["h"] * sh)
    x, y = ev(box["x"] * sw), ev(box["y"] * sh)
    w, h = min(w, sw - x), min(h, sh - y)
    return ev(w), ev(h), x, y
