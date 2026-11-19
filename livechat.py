"""Real-time chat readers for live streams. Each reader calls on_msg(text) for every chat message.

Twitch  — anonymous IRC (no account needed)
YouTube — yt-dlp's live_chat downloader, tailed as it grows
Kick    — Pusher websocket (best effort: Kick's API sometimes blocks scripts; then audio-only)
"""
import base64
import json
import os
import random
import re
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import config as C

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"


def channel_of(url, platform):
    if platform == "twitch":
        m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url)
        return m.group(1).lower() if m else None
    if platform == "kick":
        m = re.search(r"kick\.com/([A-Za-z0-9_\-]+)", url)
        return m.group(1).lower() if m else None
    return None


# ------------------------------------------------------------------ Twitch IRC
def twitch(channel, on_msg, stop: threading.Event, log):
    while not stop.is_set():
        try:
            s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=10)
            s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
            s.sendall(f"JOIN #{channel}\r\n".encode())
            log(f"Chat: connected to Twitch #{channel}.")
            s.settimeout(1.0)
            buf = b""
            while not stop.is_set():
                try:
                    data = s.recv(8192)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for ln in lines:
                    line = ln.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG " in line:
                        on_msg(line.split(" :", 1)[-1] if " :" in line else "")
            s.close()
        except Exception as e:
            if stop.is_set():
                return
            log(f"Chat: Twitch reconnecting ({str(e)[:60]})")
            stop.wait(5)


# ------------------------------------------------------------------ YouTube (yt-dlp live_chat tail)
def youtube(url, on_msg, stop: threading.Event, log, work: Path):
    work.mkdir(parents=True, exist_ok=True)
    out = work / "chat"
    cmd = [C.YTDLP, "--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(out), url]
    if C.COOKIES.exists():
        cmd[1:1] = ["--cookies", str(C.COOKIES)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    log("Chat: following YouTube live chat.")
    f, pos = None, 0
    try:
        while not stop.is_set():
            if f is None:
                cand = list(work.glob("chat*.live_chat.json*"))
                if cand:
                    f = cand[0]
            if f and f.exists():
                with open(f, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos += len(chunk)
                for line in chunk.decode("utf-8", "replace").splitlines():
                    for m in re.finditer(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', line):
                        on_msg(m.group(1))
                    if '"addChatItemAction"' in line and '"text"' not in line:
                        on_msg("")            # emoji-only messages still count
            if proc.poll() is not None and not stop.is_set():
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                log("Chat: YouTube chat stopped" + (" — bot check, add cookies.txt" if "not a bot" in err else "") + ".")
                return
            stop.wait(1.0)
    finally:
        if proc.poll() is None:
            proc.terminate()


# ------------------------------------------------------------------ Kick (Pusher websocket)
KICK_PUSHER = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0&flash=false"


def _ws_connect(url):
    m = re.match(r"wss://([^/]+)(/.*)", url)
    host, path = m.group(1), m.group(2)
    raw = socket.create_connection((host, 443), timeout=10)
    s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(1024)
    if b" 101 " not in resp.split(b"\r\n")[0]:
        raise ConnectionError("websocket handshake failed")
    return s, resp.split(b"\r\n\r\n", 1)[1]


def _ws_send(s, text):
    data = text.encode()
    mask = os.urandom(4)
    hdr = bytes([0x81])
    n = len(data)
    hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_frames(s, buf):
    """Yield text payloads forever (server frames are unmasked)."""
    def need(k):
        nonlocal buf
        while len(buf) < k:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
    while True:
        need(2)
        op, ln = buf[0] & 0x0F, buf[1] & 0x7F
        off = 2
        if ln == 126:
            need(4); ln = struct.unpack(">H", buf[2:4])[0]; off = 4
        elif ln == 127:
            need(10); ln = struct.unpack(">Q", buf[2:10])[0]; off = 10
        need(off + ln)
        payload, buf = buf[off:off + ln], buf[off + ln:]
        if op == 0x9:          # ping -> pong
            s.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
        elif op == 0x8:
            raise ConnectionError("closed")
        elif op == 0x1:
            yield payload.decode("utf-8", "replace")


def kick(slug, on_msg, stop: threading.Event, log):
    try:
        req = urllib.request.Request(f"https://kick.com/api/v2/channels/{slug}", headers={"User-Agent": UA,
                                                                                        "Accept": "application/json"})
        room = json.loads(urllib.request.urlopen(req, timeout=10).read())["chatroom"]["id"]
    except Exception as e:
        log(f"Chat: couldn't reach Kick chat ({str(e)[:60]}) — using audio and reactions only.")
        return
    while not stop.is_set():
        try:
            s, rest = _ws_connect(KICK_PUSHER)
            _ws_send(s, json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
            log(f"Chat: connected to Kick #{slug}.")
            s.settimeout(30)
            for msg in _ws_frames(s, rest):
                if stop.is_set():
                    break
                if "ChatMessageEvent" in msg:
                    try:
                        on_msg(json.loads(json.loads(msg)["data"]).get("content", ""))
                    except Exception:
                        on_msg("")
            s.close()
        except Exception as e:
            if stop.is_set():
                return
            log(f"Chat: Kick reconnecting ({str(e)[:60]})")
            stop.wait(5)


def start(platform, url, on_msg, stop, log, work: Path):
    if platform == "twitch":
        target, args = twitch, (channel_of(url, "twitch"), on_msg, stop, log)
    elif platform == "kick":
        target, args = kick, (channel_of(url, "kick"), on_msg, stop, log)
    elif platform == "youtube":
        target, args = youtube, (url, on_msg, stop, log, work)
    else:
        return None
    if args[0] is None:
        log("Chat: couldn't read the channel name from the link — audio and reactions only.")
        return None
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return t
