"""Paths and tool locations for ClipForge."""
import os
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent                       # "AI video edit"
FONTS = APP / "fonts"
STATIC = APP / "static"
DATA = APP / "data"
JOBS = DATA / "jobs"
UPLOADS = DATA / "uploads"
THUMBS = DATA / "thumbs"
GAMEPLAY = ROOT / "gameplay"
MUSIC = ROOT / "music"
BRAND = ROOT / "brand"
EXPORT = ROOT / "export"
COOKIES = APP / "cookies.txt"           # optional: Netscape cookies file for yt-dlp

for d in (DATA, JOBS, UPLOADS, THUMBS, GAMEPLAY, MUSIC, BRAND, EXPORT):
    d.mkdir(parents=True, exist_ok=True)


def _user_env(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:  # Windows: read the user-level env var even if this process predates it
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return default


_TOOLS = Path.home() / "tools" / "whisper.cpp"
WHISPER_CLI = Path(_user_env("WHISPER_CLI", str(_TOOLS / "bin" / "Release" / "whisper-cli.exe")))
WHISPER_MODEL = _user_env("WHISPER_MODEL", str(_TOOLS / "models" / "ggml-large-v3-turbo-q5_0.bin"))

# Impact and Segoe UI Black ship with Windows but may not be redistributed,
# so they're copied from the system on first run instead of living in the repo.
for _f in ("impact.ttf", "seguibl.ttf"):
    _src = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / _f
    if not (FONTS / _f).exists() and _src.exists():
        try:
            FONTS.mkdir(exist_ok=True)
            (FONTS / _f).write_bytes(_src.read_bytes())
        except OSError:
            pass
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
YTDLP = "yt-dlp"
CLAUDE = "claude"

# low-RAM machine: keep every ffmpeg call small
FF_THREADS = ["-threads", "2", "-filter_threads", "1"]
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
