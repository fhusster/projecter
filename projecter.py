"""
Projecter - cast YouTube and web video to the TVs on your network.

Supports DLNA TVs (e.g. Sanyo) and Roku TVs (through the free "Media Assistant"
channel, or Roku's own YouTube app).

Run:   python projecter.py        then open http://localhost:8765
Needs: yt-dlp (pip), ffmpeg + ffprobe on PATH, Node.js (for YouTube).
"""
import bisect
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import yt_dlp

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
WORK = os.path.join(tempfile.gettempdir(), "projecter")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

MEDIA_ASSISTANT = "782875"   # free Roku channel that plays a URL sent over the network
ROKU_YOUTUBE = "837"
CHUNK = 4 * 1024 * 1024      # YouTube throttles long single requests; fetch in chunks instead
SIZE_RATIO = 1.07            # MPEG-TS overhead vs. the source files (refined in testing)
NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184
NULLS = NULL_PACKET * 348

# "File" mode: the TV sees a seekable file, which enables its own pause/seek (and its remote).
FILE_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
# "Live" mode: an endless stream (live broadcasts).
LIVE_FLAGS = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"

FORMAT = (
    "bv*[vcodec^=avc1][height<=1080][fps<=30]+ba[acodec^=mp4a]/"
    "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/"
    "b[vcodec^=avc1][height<=1080]/"
    "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
)
YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")


DEBUG = "--debug" in sys.argv
LOG_PATH = os.path.join(HERE, "projecter.log")


def log(*a):
    line = " ".join([time.strftime("%H:%M:%S")] + [str(x) for x in a])
    if sys.stdout:
        print(line, flush=True)
    else:  # started in the background (pythonw, e.g. by the browser extension)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ============================================================================ config

CFG_LOCK = threading.Lock()


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.pop("tv", None)  # from the first version
    for k in ("names", "youtube_app"):
        cfg.setdefault(k, {})
    return cfg


CONFIG = load_config()


def save_config():
    with CFG_LOCK:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)


# ============================================================================ helpers

def http_get(url, headers=None, timeout=5, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local_ip_for(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def hms(t):
    t = max(0, int(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def hms_to_s(t):
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return 0.0


def tag(xml, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", xml or "", re.S)
    return m.group(1) if m else ""


def fetch_range(url, headers, a, b, timeout=10, deadline=25):
    """Download bytes a..b. A connection that goes quiet or crawls is dropped and retried."""
    h = dict(headers)
    h["Range"] = f"bytes={a}-{b}"
    last = None
    for _ in range(5):
        t0 = time.time()
        try:
            buf = bytearray()
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                while True:
                    c = r.read(256 * 1024)
                    if not c:
                        break
                    buf += c
                    if time.time() - t0 > deadline:
                        raise TimeoutError("download too slow")
            if len(buf) != b - a + 1:
                raise IOError(f"got {len(buf)} of {b - a + 1} bytes")
            return bytes(buf)
        except Exception as e:
            last = e
            log(f"chunk {a}-{b} retry: {e}")
            time.sleep(0.5)
    raise last


def probe_size(url, headers):
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=10) as r:
            m = re.search(r"/(\d+)", r.headers.get("Content-Range") or "")
            return int(m.group(1)) if r.status == 206 and m else None
    except Exception:
        return None


def probe_duration(url, headers):
    cmd = ["ffprobe", "-v", "error"]
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    if hdr:
        cmd += ["-headers", hdr]
    cmd += ["-show_entries", "format=duration", "-of", "csv=p=0", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
        return float(out.strip())
    except Exception:
        return 0.0


# ============================================================================ finding the video

class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ydl_opts(**kw):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": QuietLogger(),
         # Node solves YouTube's JS challenges; without it only low qualities are offered.
         "js_runtimes": {"node": {}}, "remote_components": ["ejs:github"]}
    o.update(kw)
    return o


RESOLVED = {}


def resolve(url):
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    hit = RESOLVED.get(url)
    if hit and time.time() - hit[0] < 3000:  # stream URLs expire after a few hours
        return hit[1]
    try:
        with yt_dlp.YoutubeDL(ydl_opts(format=FORMAT)) as y:
            info = y.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise RuntimeError("No video found on that page.")
            info = entries[0]
        media = media_from_info(info, url)
    except Exception as e:
        log("yt-dlp could not read it:", str(e)[:200])
        media = sniff_page(url)
        if not media:
            msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", str(e)).split("\n")[0]
            raise RuntimeError(f"Couldn't find a video there. {msg[:180]}")
    RESOLVED[url] = (time.time(), media)
    return media


def media_from_info(info, url):
    fmts = info.get("requested_formats") or [info]
    sources = []
    for f in fmts:
        sources.append({
            "url": f["url"], "headers": f.get("http_headers") or {},
            "protocol": f.get("protocol") or "", "vcodec": f.get("vcodec") or "",
            "acodec": f.get("acodec") or "",
        })
    if len(fmts) == 1 and info.get("vcodec") and not sources[0]["vcodec"]:
        sources[0]["vcodec"] = info.get("vcodec")
    page = info.get("webpage_url") or url
    yt = YT_ID.search(page)
    extractor = (info.get("extractor_key") or "").lower()
    subs = []
    for lang, tracks in (info.get("subtitles") or {}).items():
        if lang == "live_chat":
            continue
        best = next((t for ext in ("vtt", "srt", "ass") for t in tracks if t.get("ext") == ext and t.get("url")), None)
        if best:
            subs.append({"label": best.get("name") or lang, "lang": lang, "url": best["url"],
                         "headers": sources[0]["headers"]})
    return {
        "subtitles": subs, "sub": -1,
        "key": page,
        "title": info.get("title") or "Video",
        "thumb": info.get("thumbnail"),
        "duration": float(info.get("duration") or 0),
        "is_live": bool(info.get("is_live")) or not info.get("duration"),
        "site": {"Youtube": "YouTube"}.get(info.get("extractor_key"), info.get("extractor_key"))
        or urllib.parse.urlparse(page).netloc,
        "youtube_id": yt.group(1) if yt and extractor.startswith("youtube") else None,
        "quality": f"{fmts[0].get('height')}p" if fmts[0].get("height") else "",
        "sources": sources,
    }


def sniff_page(url):
    """Fallback for sites yt-dlp doesn't know: look for a .m3u8 / .mp4 link in the page."""
    try:
        page = http_get(url, headers={"User-Agent": UA}, timeout=12).decode("utf-8", "replace")
    except Exception:
        return None
    text = page.replace("\\/", "/").replace("&amp;", "&")
    found = re.findall(r"https?://[^\s\"'<>()\\]+?\.(?:m3u8|mp4)(?:\?[^\s\"'<>()\\]*)?", text)
    if not found:
        return None
    found.sort(key=lambda u: 0 if ".m3u8" in u else 1)
    src = found[0]
    headers = {"User-Agent": UA, "Referer": url}
    dur = probe_duration(src, headers)
    t = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page, re.I)
    return {
        "key": src,
        "title": (t.group(1).strip() if t else urllib.parse.urlparse(url).netloc)[:200],
        "thumb": og.group(1) if og else None,
        "duration": dur, "is_live": not dur,
        "site": urllib.parse.urlparse(url).netloc, "youtube_id": None, "quality": "",
        "sources": [{"url": src, "headers": headers,
                     "protocol": "m3u8" if ".m3u8" in src else "https", "vcodec": "", "acodec": ""}],
    }


def search(q):
    with yt_dlp.YoutubeDL(ydl_opts(extract_flat=True)) as y:
        info = y.extract_info(f"ytsearch16:{q}", download=False)
    out = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        if not vid or e.get("ie_key", "Youtube") not in ("Youtube", None):
            continue
        out.append({
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration") or 0,
            "live": e.get("live_status") == "is_live",
            "views": e.get("view_count") or 0,
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return out


# ============================================================================ sessions
# A session repackages one video into a local MPEG-TS file (plus an HLS index of it).
# DLNA TVs read that file directly; Rokus read it as HLS. Everything after the first
# download comes from local disk, so pausing and skipping are fast.

SESSIONS = {}
SESS_LOCK = threading.Lock()


class Session:
    def __init__(self, media):
        self.id = uuid.uuid4().hex[:8]
        self.media = media
        self.dir = os.path.join(WORK, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.ts = os.path.join(self.dir, "media.ts")
        self.m3u8 = os.path.join(self.dir, "index.m3u8")
        self.segs = []          # (start_time, duration, byte_offset, byte_length)
        self.starts = []
        self.written = 0
        self.done = False
        self.closed = False
        self.error = None
        self.proc = None
        self.vsize = None
        self.cond = threading.Condition()
        self.start_lock = threading.Lock()
        self.used = time.time()
        self.sub_file = None      # subtitles drawn into the picture (TVs can't load them separately)
        self.sub_error = None
        self.vbitrate = None      # set when re-encoding: makes the final size predictable
        self.base = 0.0           # where in the video this session starts (only >0 when re-encoding)

    # ---- remuxing
    def start(self):
        with self.start_lock:
            if self.proc:
                return
            for s in self.media["sources"]:
                if s["protocol"] in ("http", "https"):
                    s["size"] = probe_size(s["url"], s["headers"])
                    s["proxy"] = bool(s["size"])
            if sub_on(self.media):
                try:
                    self.sub_file = self.prepare_subtitles()
                except Exception as e:
                    self.sub_error = f"Subtitles couldn't be loaded ({str(e)[:80]}), playing without them."
                    log(self.sub_error)
            cmd = self.ffmpeg_cmd()
            self.errfile = open(os.path.join(self.dir, "ffmpeg.log"), "wb")
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, cwd=self.dir,
                                         stderr=self.errfile, creationflags=NO_WINDOW)
            threading.Thread(target=self._watch, daemon=True).start()
            log(f"session {self.id}: {self.media['title'][:60]}")

    def prepare_subtitles(self):
        """Load the chosen track, apply the timing slider, and save it as final.srt."""
        raw = fetch_subtitle(self.media["subtitles"][self.media["sub"]], self.dir)
        if raw.endswith(".ass"):
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", raw, "conv.srt"],
                           cwd=self.dir, capture_output=True, timeout=30, creationflags=NO_WINDOW, check=True)
            raw = "conv.srt"
        with open(os.path.join(self.dir, raw), encoding="utf-8", errors="replace") as f:
            cues = parse_cues(f.read())
        if not cues:
            raise RuntimeError("no subtitle lines found in it")
        style = dict(SUB_STYLE, **(self.media.get("sub_style") or {}))
        # The video starts at self.base (we encode from where you're watching), so shift too.
        shift = float(style["delay"]) - self.base
        out, n = [], 0
        for a, b, text in cues:
            a, b = a + shift, b + shift
            if b <= 0:
                continue
            n += 1
            out.append(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n")
        with open(os.path.join(self.dir, "final.srt"), "w", encoding="utf-8") as f:
            f.write("\n".join(out) or "1\n00:00:00,000 --> 00:00:00,001\n \n")
        return "final.srt"

    def ffmpeg_cmd(self):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        srcs = self.media["sources"]
        seek = ["-ss", f"{self.base:.3f}"] if self.base else []
        for i, s in enumerate(srcs):
            if s.get("proxy"):
                cmd += seek + ["-i", f"http://127.0.0.1:{PORT}/src/{self.id}/{i}"]
                continue
            hdr = "".join(f"{k}: {v}\r\n" for k, v in s["headers"].items())
            if hdr:
                cmd += ["-headers", hdr]
            if not s["protocol"].startswith("m3u8"):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            cmd += seek + ["-i", s["url"]]
        if len(srcs) > 1:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
        probe_sources(self.media)
        a = srcs[-1]["acodec"]
        if self.sub_file:
            # Draw the subtitles into the picture. 720p keeps encoding ~5x faster than playback.
            style = sub_force_style(dict(SUB_STYLE, **(self.media.get("sub_style") or {})))
            self.vbitrate = 3500
            cmd += ["-vf", f"scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,subtitles={self.sub_file}:force_style='{style}'"]
        elif not video_ok(srcs[0]):
            # TVs (Rokus especially) only decode 8-bit H.264 up to level 4.2
            self.vbitrate = 5000
            cmd += ["-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"]
        if self.vbitrate:
            b = self.vbitrate
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{b}k", "-maxrate", f"{b}k",
                    "-bufsize", f"{b * 2}k", "-g", "60", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        else:
            cmd += ["-c:v", "copy"]
            # Rokus play only AAC inside HLS (AC-3/E-AC-3/Opus/MP3 give a silent black screen).
            # Unknown audio is converted too: it's cheap and always safe.
            if a != "none" and not re.match(r"(mp4a|aac)", a or ""):
                cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
            else:
                cmd += ["-c:a", "copy"]
        cmd += ["-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                "-hls_flags", "single_file+independent_segments", "-hls_segment_type", "mpegts",
                "-hls_segment_filename", self.ts]
        if not self.media["is_live"]:
            cmd += ["-hls_playlist_type", "event"]
        return cmd + [self.m3u8]

    def _parse(self):
        try:
            with open(self.m3u8, encoding="utf-8") as f:
                txt = f.read()
        except OSError:
            return
        segs, t, dur, br = [], self.base, None, None
        for line in txt.splitlines():
            if line.startswith("#EXTINF:"):
                dur = float(line[8:].split(",")[0])
            elif line.startswith("#EXT-X-BYTERANGE:"):
                ln, _, off = line[17:].partition("@")
                br = (int(off or 0), int(ln))
            elif line and not line.startswith("#") and dur is not None and br:
                segs.append((t, dur, br[0], br[1]))
                t += dur
                dur = br = None
        try:
            written = os.path.getsize(self.ts)
        except OSError:
            written = 0
        with self.cond:
            if len(segs) >= len(self.segs):
                self.segs = segs
                self.starts = [s[0] for s in segs]
            self.written = max(self.written, written)
            self.cond.notify_all()

    def _watch(self):
        while not self.closed:
            alive = self.proc.poll() is None
            self._parse()
            if not alive:
                time.sleep(0.3)
                self._parse()
                with self.cond:
                    self.done = True
                    if not self.segs:
                        try:
                            with open(os.path.join(self.dir, "ffmpeg.log"), encoding="utf-8", errors="replace") as f:
                                err = f.read().strip().splitlines()
                        except OSError:
                            err = []
                        self.error = (err[-1] if err else "the video could not be read")[:200]
                    self.cond.notify_all()
                if self.segs and not self.media["is_live"]:
                    src = sum(s.get("size") or 0 for s in self.media["sources"])
                    log(f"session {self.id}: done, {self.written / 1e6:.0f} MB"
                        + (f" (ratio {self.written / src:.3f})" if src else ""))
                return
            time.sleep(0.25)

    # ---- waiting for data
    def wait(self, pred, timeout=45, what="the video"):
        end = time.time() + timeout
        with self.cond:
            while not pred():
                if self.error:
                    raise RuntimeError(f"Couldn't play {what}: {self.error}")
                if self.done or self.closed:
                    break
                if time.time() > end:
                    raise RuntimeError("The video is taking too long to load.")
                self.cond.wait(0.5)
            if self.error:
                raise RuntimeError(f"Couldn't play {what}: {self.error}")

    def wait_segments(self, n, timeout=45):
        self.wait(lambda: len(self.segs) >= n, timeout)
        if not self.segs:
            raise RuntimeError("No video data arrived.")

    def seg_index(self, t, timeout=90):
        """Index of the segment that contains time t (waits until it exists)."""
        self.wait(lambda: self.segs and self.segs[-1][0] + self.segs[-1][1] > t, timeout)
        if not self.segs:
            raise RuntimeError("No video data arrived.")
        return max(0, bisect.bisect_right(self.starts, t) - 1)

    def seg_for_byte(self, b):
        offs = [s[2] for s in self.segs]
        return max(0, bisect.bisect_right(offs, b) - 1)

    def virtual_size(self):
        """Size we announce to DLNA TVs (the real size isn't known until the download ends)."""
        if self.vsize or self.media["is_live"]:
            return self.vsize
        srcs = self.media["sources"]
        if self.vbitrate:
            # re-encoding at a capped bitrate: the size is (bitrate x duration) plus TS overhead
            est = (self.vbitrate + 160) * 1000 / 8 * (self.media["duration"] - self.base) * 1.08 + 1_000_000
        elif all(s.get("size") for s in srcs):
            est = sum(s["size"] for s in srcs) * SIZE_RATIO + 1_000_000
        else:
            dur = self.media["duration"]
            self.wait(lambda: self.segs and self.segs[-1][0] + self.segs[-1][1] >= min(20, dur * 0.9), 60)
            if self.done:
                est = self.written
            else:
                s = self.segs[-1]
                est = (s[2] + s[3]) / (s[0] + s[1]) * dur * 1.15
        self.vsize = int(est) // 188 * 188
        return self.vsize

    def cached_until(self):
        if not self.segs:
            return 0.0
        s = self.segs[-1]
        return s[0] + s[1]

    # ---- serving
    def pump(self, wfile, pos, length=None):
        """Send bytes from the growing file, following it as it's written; pad with nulls."""
        sent = 0
        with open(self.ts, "rb") as f:
            f.seek(pos)
            while length is None or sent < length:
                if self.closed:
                    return
                n = 65536 if length is None else min(65536, length - sent)
                chunk = f.read(n)
                if chunk:
                    wfile.write(chunk)
                    sent += len(chunk)
                    continue
                if self.done:
                    break
                with self.cond:
                    self.cond.wait(0.5)
                f.seek(f.tell())
        while length is not None and sent < length:
            n = min(len(NULLS), length - sent)
            wfile.write(NULLS[:n])
            sent += n

    def close(self):
        self.closed = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except Exception:
                pass
        with self.cond:
            self.cond.notify_all()
        try:
            self.errfile.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


SUB_STYLE = {"size": 20, "bg": 0, "pos": 6, "delay": 0.0}


def sub_on(media):
    i = media.get("sub", -1)
    return i is not None and 0 <= i < len(media.get("subtitles") or []) and not media["is_live"]


def probe_sources(media):
    """Fill in what's actually inside each source (codec, bit depth, H.264 level). Streams found
    by the browser extension arrive with no codec info at all; guessing 'it's fine' is what
    gave Rokus a black screen. Results are cached on the source dicts."""
    for s in media["sources"]:
        if s.get("probed"):
            continue
        s["probed"] = True
        cmd = ["ffprobe", "-v", "error"]
        hdr = "".join(f"{k}: {v}\r\n" for k, v in s["headers"].items())
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-show_entries", "stream=codec_type,codec_name,profile,level,pix_fmt,width,height",
                "-of", "json", s["url"]]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW)
            streams = json.loads(out.stdout or "{}").get("streams") or []
        except Exception as e:
            log(f"probe failed: {str(e)[:80]}")
            continue
        v = next((x for x in streams if x.get("codec_type") == "video"), None)
        a = next((x for x in streams if x.get("codec_type") == "audio"), None)
        if v and s.get("vcodec") != "none":
            s["vcodec"] = v.get("codec_name") or s.get("vcodec") or ""
            s["vprofile"] = v.get("profile") or ""
            s["vlevel"] = v.get("level") or 0
            s["pix_fmt"] = v.get("pix_fmt") or ""
            s["width"], s["height"] = v.get("width") or 0, v.get("height") or 0
        if a:
            s["acodec"] = a.get("codec_name") or s.get("acodec") or ""
        elif s.get("vcodec") != "none" and len(media["sources"]) == 1 and streams:
            s["acodec"] = "none"
        log(f"stream: video {s.get('vcodec') or '?'} {s.get('vprofile', '')} L{s.get('vlevel', '?')} "
            f"{s.get('pix_fmt', '')}, audio {s.get('acodec') or '?'}")


def video_ok(src):
    """Can the TV decode this video as-is? Only plain 8-bit H.264 up to level 4.2."""
    v = src.get("vcodec") or ""
    if v == "none":
        return True
    if not re.match(r"(avc|h264)", v):
        return False
    if src.get("pix_fmt") and src["pix_fmt"] not in ("yuv420p", "yuvj420p"):
        return False   # 10-bit or 4:2:2/4:4:4
    if re.search(r"10|422|444", src.get("vprofile") or ""):
        return False
    try:
        if (src.get("width") or 0) > 1920 or (src.get("height") or 0) > 1080:
            return False   # e.g. 2600x1080 wide-screen encodes: Rokus show black
        return int(src.get("vlevel") or 0) <= 42
    except (TypeError, ValueError):
        return True


def needs_transcode(media):
    if sub_on(media):
        return True
    probe_sources(media)
    return not video_ok(media["sources"][0])


def session_key(media):
    """Same video + same subtitles + same subtitle style = same session (switching TVs is instant)."""
    if not sub_on(media):
        return media["key"]
    s = media["subtitles"][media["sub"]]
    ident = s.get("url") or hashlib.md5((s.get("text") or "").encode()).hexdigest()
    style = json.dumps(media.get("sub_style") or SUB_STYLE, sort_keys=True)
    return f"{media['key']}|sub={ident}|{style}"


CUE_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")


def parse_cues(text):
    """Read SRT or WebVTT text into [(start, end, text)]."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            m = CUE_RE.search(line)
            if m:
                g = [int(x or 0) for x in m.groups()]
                a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                body = "\n".join(lines[i + 1:]).strip()
                body = re.sub(r"</?(c|v|lang|ruby|rt)[^>]*>", "", body)   # WebVTT-only tags
                if body:
                    cues.append((a, b, body))
                break
    return cues


def srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def sub_force_style(st):
    """Slider values -> libass style. Sizes and margins are on libass's 288-line canvas."""
    style = [f"FontName=Arial", f"FontSize={int(st['size'])}", "Alignment=2",
             f"MarginV={int(8 + float(st['pos']) / 100 * 230)}", "PrimaryColour=&H00FFFFFF"]
    bg = float(st["bg"])
    if bg > 0:
        alpha = int(round(255 * (1 - bg / 100)))
        style += ["BorderStyle=3", f"OutlineColour=&H{alpha:02X}000000", "BackColour=&H{0:02X}000000".format(alpha),
                  "Outline=2", "Shadow=0"]
    else:
        style += ["BorderStyle=1", "OutlineColour=&H00000000", "Outline=1.6", "Shadow=0.6"]
    return ",".join(style)


def fetch_subtitle(sub, folder):
    """Save a subtitle track into the session folder; returns its file name."""
    if sub.get("text"):
        data = sub["text"]
    elif re.search(r"\.m3u8(\?|$)", sub.get("url", ""), re.I):
        # HLS subtitle playlists are cut into pieces: let ffmpeg join them into one file
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (sub.get("headers") or {}).items())
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-i", sub["url"], "-f", "webvtt", "sub.vtt"]
        subprocess.run(cmd, cwd=folder, capture_output=True, timeout=60, creationflags=NO_WINDOW, check=True)
        return "sub.vtt"
    else:
        raw = http_get(sub["url"], headers=sub.get("headers") or {}, timeout=15)
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                data = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    data = data.replace("\r\n", "\n").lstrip("\ufeff")
    if "#xywh=" in data:
        raise RuntimeError("that's a preview-thumbnail track, not subtitles")
    if data.startswith("WEBVTT"):
        ext = "vtt"
    elif "[Script Info]" in data:
        ext = "ass"
    elif re.search(r"\d+:\d\d:\d\d,\d{3}\s*-->", data):
        ext = "srt"
    elif "-->" in data:
        ext, data = "vtt", "WEBVTT\n\n" + data
    else:
        raise RuntimeError("not a subtitle file")
    name = f"sub.{ext}"
    with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
        f.write(data)
    return name


def get_session(media, start=0.0):
    """Find or start the session for this video. Plain copies always start at 0 (they finish in
    seconds). Re-encoded ones (subtitles) start where you're watching, so there's no wait."""
    transcode = needs_transcode(media)
    with SESS_LOCK:
        for s in SESSIONS.values():
            if session_key(s.media) != session_key(media) or s.closed or s.error:
                continue
            # a re-encode must already cover `start`, or be close to reaching it
            if transcode and not (s.base <= start + 0.5 and start - max(s.cached_until(), s.base) < 45):
                continue
            s.used = time.time()
            return s
        s = Session(media)
        if transcode and start > 3:
            s.base = max(0.0, start - 1.0)
        SESSIONS[s.id] = s
        in_use = {d.session.id for d in DEVICES.values() if d.session}
        old = sorted((x for x in SESSIONS.values() if x.id not in in_use and x is not s),
                     key=lambda x: x.used)
        # Re-encodes (subtitles) eat the processor: stop unused ones right away. Plain copies
        # are cheap: keep the most recent one around for quick re-casts.
        spare = [x for x in old if not x.vbitrate][-1:]
        for x in old:
            if x not in spare:
                x.close()
                SESSIONS.pop(x.id, None)
        return s


def release_session(s, keep=None):
    """Stop a re-encode nobody is watching any more (it would slow the next one down)."""
    if not s or s is keep or not s.vbitrate:
        return
    with SESS_LOCK:
        if any(d.session is s for d in DEVICES.values()):
            return
        s.close()
        SESSIONS.pop(s.id, None)


# ============================================================================ devices

DEVICES = {}
DEV_LOCK = threading.Lock()


class Device:
    kind = "tv"

    def __init__(self, dev_id, default_name, ip):
        self.id = dev_id
        self.default_name = default_name
        self.ip = ip
        self.last_seen = time.time()
        self.lock = threading.RLock()
        self.session = None
        self.media = None
        self.offset = 0.0
        self.mode = None
        self.busy = None
        self.notice = None

    @property
    def name(self):
        return CONFIG["names"].get(self.id) or self.default_name

    @property
    def online(self):
        return time.time() - self.last_seen < 40

    def host(self):
        return local_ip_for(self.ip)

    def info(self):
        return {"id": self.id, "name": self.name, "kind": self.kind, "online": self.online,
                "status": self.availability(), "warning": self.warning(),
                "playing": bool(self.media), "title": self.media and self.media["title"]}

    def availability(self):
        return "online" if self.online else "offline"

    def warning(self):
        return None

    def set_playing(self, media, session, mode, offset=0.0):
        prev = self.session
        self.media, self.session, self.mode, self.offset = media, session, mode, offset
        release_session(prev, keep=session)

    def clear(self):
        prev = self.session
        self.media = self.session = self.mode = None
        self.offset = 0.0
        release_session(prev)

    def player(self):
        if not self.media:
            return {"busy": self.busy} if self.busy else None
        m = self.media
        st = {"title": m["title"], "thumb": m["thumb"], "site": m["site"], "quality": m["quality"],
              "duration": m["duration"], "live": m["is_live"], "busy": self.busy, "mode": self.mode,
              "cached": min(1.0, self.session.cached_until() / m["duration"])
              if self.session and m["duration"] else (1.0 if self.mode == "youtube" else 0),
              # subtitles can be switched when the video goes through the laptop
              "subtitles": [s.get("label") or s.get("lang") or f"Track {i + 1}" for i, s in enumerate(m.get("subtitles") or [])],
              "sub": m.get("sub", -1) if self.session and self.session.sub_file else -1,
              "subs_ok": self.mode in ("file", "hls") and not m["is_live"],
              "sub_style": dict(SUB_STYLE, **(m.get("sub_style") or {}))}
        if self.session and self.session.sub_error:
            st["notice"] = self.session.sub_error
        try:
            st.update(self.status())
        except Exception as e:
            st.update({"state": "unknown", "position": 0, "error": str(e)[:120]})
        return st

    def switch_subtitles(self, index, extra=None, style=None):
        """Turn subtitles off (-1), pick a track, add one (extra = {label, text}), or restyle."""
        m = dict(self.media)
        if style:
            m["sub_style"] = style
        subs = list(m.get("subtitles") or [])
        if extra:
            subs.append({"label": (extra.get("label") or "My subtitles")[:60], "text": extra.get("text") or ""})
            index = len(subs) - 1
        if index >= len(subs):
            raise RuntimeError("That subtitle track is gone.")
        m["subtitles"], m["sub"] = subs, index
        try:
            pos = float(self.status().get("position") or 0)
        except Exception:
            pos = 0.0
        self.busy = "Adding subtitles..." if index >= 0 else "Removing subtitles..."
        try:
            self.cast(m, start=pos if pos > 3 else 0)
        finally:
            self.busy = None


# ---------------------------------------------------------------------------- DLNA (Sanyo etc.)

class DlnaDevice(Device):
    kind = "dlna"

    def __init__(self, dev_id, name, ip, location, avt, rc):
        super().__init__(dev_id, name, ip)
        self.location, self.avt_url, self.rc_url = location, avt, rc
        self.paused_at = None     # pause emulation for live streams
        self.vol_cache = (0, None, None)
        self.cast_time = 0
        self.seek_target = self.seek_pos = self.seek_thread = None
        self.seek_since = 0

    def soap(self, url, service, action, args, timeout=5):
        service = f"urn:schemas-upnp-org:service:{service}:1"
        body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
        env = ('<?xml version="1.0" encoding="utf-8"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>').encode()
        req = urllib.request.Request(url, data=env, method="POST", headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = r.read().decode("utf-8", "replace")
            if DEBUG and (action not in ("GetTransportInfo", "GetPositionInfo", "GetVolume", "GetMute")
                          or time.time() - t0 > 1):
                log(f"[soap] {action} {time.time() - t0:.2f}s")
            return out
        except urllib.error.HTTPError as e:
            code = tag(e.read().decode("utf-8", "replace"), "errorCode")
            raise RuntimeError(f"The TV refused {action} (error {code})")
        except (socket.timeout, TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if DEBUG:
                log(f"[soap] {action} FAILED after {time.time() - t0:.2f}s: {e!r}")
            # These TVs pick a new port whenever their media player restarts: find it and retry.
            if not getattr(self, "_relocating", False) and not isinstance(e, (socket.timeout, TimeoutError)):
                self._relocating = True
                try:
                    if self.relocate():
                        url = self.avt_url if "AVTransport" in service else self.rc_url
                        return self.soap(url, service.split(":")[-2], action, args, timeout)
                finally:
                    self._relocating = False
            raise RuntimeError(f"{self.name} isn't responding. Is it on?")

    def relocate(self):
        for ip, h in ssdp_search(2.5):
            loc = h.get("location")
            if ip == self.ip and loc and loc != self.location:
                try:
                    desc = describe_renderer(loc)
                except Exception:
                    continue
                if desc:
                    self.location, self.avt_url, self.rc_url = loc, desc["avt"], desc["rc"]
                    log(f"{self.name} moved to {loc}")
                    return True
        return False

    def avt(self, action, timeout=15, **args):
        # Actions get a long timeout: the TV can take several seconds to confirm a Play
        # after a pause, even though it starts playing right away.
        return self.soap(self.avt_url, "AVTransport", action, {"InstanceID": 0, **args}, timeout)

    def rc(self, action, **args):
        return self.soap(self.rc_url, "RenderingControl", action, {"InstanceID": 0, "Channel": "Master", **args})

    def cast(self, media, start=0.0):
        s = get_session(media, start)
        s.start()
        s.wait_segments(1)
        host = self.host()
        nonce = uuid.uuid4().hex[:6]
        if media["is_live"]:
            k = s.seg_index(start) if start else max(0, len(s.segs) - 2)
            url = f"http://{host}:{PORT}/dlna/{s.id}/live-{nonce}.ts?from={k}"
            res = f'<res protocolInfo="http-get:*:video/mpeg:{LIVE_FLAGS}">{escape(url)}</res>'
            mode, offset = "live", s.segs[k][0]
        else:
            vs = s.virtual_size()
            url = f"http://{host}:{PORT}/dlna/{s.id}/file-{nonce}.ts"
            # a re-encoded session may start partway in (s.base): the TV's file is the rest
            res = (f'<res protocolInfo="http-get:*:video/mpeg:{FILE_FLAGS}" size="{vs}" '
                   f'duration="{hms(media["duration"] - s.base)}.000">{escape(url)}</res>')
            mode, offset = "file", s.base
        didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                '<item id="0" parentID="-1" restricted="1">'
                f'<dc:title>{escape(media["title"])}</dc:title>'
                f'<upnp:class>object.item.videoItem</upnp:class>{res}</item></DIDL-Lite>')
        try:
            self.avt("Stop", timeout=3)
        except Exception:
            pass
        self.avt("SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=didl)
        self.avt("Play", Speed=1)
        self.set_playing(media, s, mode, offset)
        self.paused_at = None
        self.seek_target = self.seek_pos = None
        self.cast_time = time.time()
        if start - s.base > 2 and mode == "file":
            self.wait_state("PLAYING", 12)
            self.seek(start)

    def wait_state(self, want, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if tag(self.avt("GetTransportInfo", timeout=3), "CurrentTransportState") == want:
                return True
            time.sleep(0.4)
        return False

    def seek(self, t):
        m = self.media
        if self.mode != "file":
            raise RuntimeError("You can't skip around in a live stream.")
        s = self.session
        t = max(0.0, min(t, m["duration"] - 3))
        if t < s.base or (s.vbitrate and t - s.cached_until() > 45):
            # Before (or far past) what this re-encode covers: start a new one from there.
            return self.cast(m, start=t)
        # The TV turns a time into a byte position with simple maths (size x t / duration).
        # Ask for the time that lands exactly on the start of the segment we want.
        i = s.seg_index(t)
        target = s.segs[i][2] / s.vsize * (m["duration"] - s.base)
        # The TV only answers a Seek once it has re-buffered (~5s), although the picture
        # moves sooner. Do it in the background; if more skips arrive, the newest one wins.
        self.seek_target, self.seek_pos, self.seek_since = hms(round(target)), s.segs[i][0], time.time()
        if not self.seek_thread:
            self.seek_thread = threading.Thread(target=self._seek_worker, daemon=True)
            self.seek_thread.start()

    def _seek_worker(self):
        while True:
            with self.lock:
                target, self.seek_target = self.seek_target, None
                if target is None:
                    self.seek_thread = None
                    return
            try:
                self.avt("Seek", timeout=15, Unit="REL_TIME", Target=target)
            except RuntimeError as e:
                log(f"seek failed: {e}")
            with self.lock:
                if self.seek_target is None:
                    self.seek_pos = None

    def pause(self):
        if self.mode == "live":
            # The TV can't pause a live stream, but we keep recording it: stop the TV and
            # resume from this exact spot later (time-shift).
            rel = hms_to_s(tag(self.avt("GetPositionInfo", timeout=3), "RelTime"))
            self.avt("Stop")
            self.paused_at = self.offset + rel
        else:
            self.avt("Pause")

    def play(self):
        if self.paused_at is not None:
            self.cast(self.media, start=self.paused_at)
        else:
            self.avt("Play", Speed=1)

    def toggle(self):
        if self.paused_at is not None:
            return self.play()
        state = tag(self.avt("GetTransportInfo", timeout=3), "CurrentTransportState")
        self.pause() if state == "PLAYING" else self.play()

    def stop(self):
        try:
            self.avt("Stop")
        finally:
            self.clear()
            self.paused_at = None

    def volume(self, action, value=None):
        if not self.rc_url:
            raise RuntimeError("This TV doesn't allow volume control over the network.")
        if action == "mute":
            muted = tag(self.rc("GetMute"), "CurrentMute") == "1"
            self.rc("SetMute", DesiredMute=0 if muted else 1)
        else:
            cur = int(tag(self.rc("GetVolume"), "CurrentVolume") or 0)
            new = value if action == "set_volume" else cur + (3 if action == "vol_up" else -3)
            self.rc("SetVolume", DesiredVolume=max(0, min(100, int(new))))
        self.vol_cache = (0, None, None)

    def read_volume(self):
        t, vol, muted = self.vol_cache
        if time.time() - t > 4 and self.rc_url:
            try:
                vol = int(tag(self.rc("GetVolume"), "CurrentVolume") or 0)
                muted = tag(self.rc("GetMute"), "CurrentMute") == "1"
            except Exception:
                pass
            self.vol_cache = (time.time(), vol, muted)
        return vol, muted

    def status(self):
        if self.seek_thread and self.seek_pos is not None:
            # The TV handles one request at a time; don't queue status checks behind a Seek.
            _, vol, muted = self.vol_cache
            return {"position": self.seek_pos, "state": "loading", "volume": vol, "muted": muted,
                    "remote": True, "can_seek": True}
        state = tag(self.avt("GetTransportInfo", timeout=3), "CurrentTransportState")
        pos_xml = self.avt("GetPositionInfo", timeout=3)
        rel = hms_to_s(tag(pos_xml, "RelTime"))
        uri = tag(pos_xml, "TrackURI")
        pos = self.offset + rel   # offset: where the TV's file starts in the video
        vol, muted = self.read_volume()
        out = {"position": pos, "volume": vol, "muted": muted,
               "remote": self.mode == "file", "can_seek": self.mode == "file"}
        if self.seek_pos is not None:
            # mid-skip: report the destination until the TV has actually moved there
            if abs(pos - self.seek_pos) < 6 and state == "PLAYING" or time.time() - self.seek_since > 20:
                self.seek_pos = None
            else:
                out.update(position=self.seek_pos, state="loading")
                return out
        if self.paused_at is not None:
            out["state"] = "paused"
            out["position"] = self.paused_at
            return out
        recent = time.time() - self.cast_time < 15
        if self.session and uri and self.session.id not in uri and not recent:
            self.clear()  # something else is playing on the TV now
            return {"state": "stopped", "position": 0}
        out["state"] = {"PLAYING": "playing", "PAUSED_PLAYBACK": "paused", "TRANSITIONING": "loading"}.get(
            state, "loading" if recent else "stopped")
        d = self.media["duration"] if self.media else 0
        if self.mode == "file" and d and pos >= d - 1.0 and state == "PLAYING":
            # The announced size is a little bigger than the real file; end cleanly.
            try:
                self.avt("Stop", timeout=3)
            except Exception:
                pass
            out["state"] = "ended"
        if out["state"] in ("stopped", "ended") and not recent:
            self.clear()
        return out


def describe_renderer(location):
    root = ET.fromstring(http_get(location, timeout=4))
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    dev = root.find(".//d:device", ns)
    if dev is None or "MediaRenderer" not in dev.findtext("d:deviceType", "", ns):
        return None
    base = root.findtext("d:URLBase", None, ns) or location
    out = {"name": dev.findtext("d:friendlyName", "TV", ns), "udn": dev.findtext("d:UDN", location, ns),
           "manufacturer": dev.findtext("d:manufacturer", "", ns), "avt": None, "rc": None}
    for svc in dev.iter("{urn:schemas-upnp-org:device-1-0}service"):
        stype = svc.findtext("d:serviceType", "", ns)
        ctrl = urllib.parse.urljoin(base, svc.findtext("d:controlURL", "", ns))
        if "AVTransport" in stype:
            out["avt"] = ctrl
        elif "RenderingControl" in stype:
            out["rc"] = ctrl
    return out if out["avt"] else None


# ---------------------------------------------------------------------------- Roku

ROKU_LIMITED = ("Your Roku is blocking network control. On the Roku: Settings > System > "
                "Advanced system settings > Control by mobile apps > Network access > Enabled.")
ROKU_NO_MA = ("Add the free 'Media Assistant' channel on your Roku (Home > Streaming Store > "
              "search 'Media Assistant'), then try again.")


class RokuDevice(Device):
    kind = "roku"

    def __init__(self, dev_id, name, ip):
        super().__init__(dev_id, name, ip)
        self.base = f"http://{ip}:8060"
        self.power = "PowerOn"
        self.ecp = "enabled"
        self.apps = set()
        self.info_time = 0
        self.cast_time = 0

    def ecp_call(self, path, method="POST", timeout=6):
        try:
            data = b"" if method == "POST" else None
            return http_get(self.base + path, data=data, method=method, timeout=timeout).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError(ROKU_LIMITED)
            raise RuntimeError(f"Roku said {e.code} to {path.split('?')[0]}")
        except (socket.timeout, TimeoutError, urllib.error.URLError):
            raise RuntimeError(f"{self.name} isn't responding. Is it on?")

    def refresh(self, force=False):
        if not force and time.time() - self.info_time < 20:
            return
        x = ET.fromstring(http_get(self.base + "/query/device-info", timeout=4))
        self.default_name = x.findtext("user-device-name") or x.findtext("friendly-device-name") or "Roku"
        self.power = x.findtext("power-mode") or "PowerOn"
        self.ecp = x.findtext("ecp-setting-mode") or "enabled"
        if self.ecp not in ("limited", "disabled"):
            try:
                apps = ET.fromstring(http_get(self.base + "/query/apps", timeout=4))
                self.apps = {a.get("id") for a in apps.iter("app")}
            except Exception:
                pass
        self.info_time = time.time()

    def availability(self):
        if not self.online:
            return "offline"
        return "online" if self.power == "PowerOn" else "standby"

    def warning(self):
        if self.ecp in ("limited", "disabled"):
            return ROKU_LIMITED
        if self.apps and MEDIA_ASSISTANT not in self.apps:
            return ROKU_NO_MA
        return None

    @property
    def youtube_app(self):
        return bool(CONFIG["youtube_app"].get(self.id))

    def info(self):
        d = super().info()
        d["youtube_app"] = self.youtube_app
        return d

    def key(self, k):
        self.ecp_call(f"/keypress/{k}")

    def wake(self):
        try:
            self.refresh(force=True)
        except Exception:
            pass
        if self.power != "PowerOn":
            self.key("PowerOn")
            time.sleep(4)
            self.power = "PowerOn"

    def cast(self, media, start=0.0):
        self.wake()
        if self.ecp in ("limited", "disabled"):
            raise RuntimeError(ROKU_LIMITED)
        if self.youtube_app and media["youtube_id"]:
            q = urllib.parse.urlencode({"contentId": media["youtube_id"], "mediaType": "movie"})
            self.ecp_call(f"/launch/{ROKU_YOUTUBE}?{q}")
            self.set_playing(media, None, "youtube", 0.0)
            self.cast_time = time.time()
            return
        if self.apps and MEDIA_ASSISTANT not in self.apps:
            raise RuntimeError(ROKU_NO_MA)
        s = get_session(media, start)
        s.start()
        if media["is_live"]:
            s.wait_segments(3)
            k = max(0, len(s.segs) - 3)
        else:
            k = s.seg_index(max(start, s.base))
        s.wait_segments(k + 3)
        url = f"http://{self.host()}:{PORT}/hls/{s.id}/{uuid.uuid4().hex[:6]}.m3u8?from={k}"
        q = urllib.parse.urlencode({"t": "v", "u": url, "videoName": media["title"][:100],
                                    "videoFormat": "hls"})
        self.ecp_call(f"/launch/{MEDIA_ASSISTANT}?{q}")
        self.set_playing(media, s, "hls", s.segs[k][0])
        self.cast_time = time.time()

    def player_xml(self):
        return ET.fromstring(self.ecp_call("/query/media-player", method="GET", timeout=3))

    def pause(self):
        if self.player_xml().get("state") == "play":
            self.key("Play")

    def play(self):
        if self.player_xml().get("state") in ("pause", "stop"):
            self.key("Play")

    def toggle(self):
        self.key("Play")

    def seek(self, t):
        if self.mode == "youtube":
            raise RuntimeError("In Roku's YouTube app, skip with the Roku remote (or turn off "
                               "'Use Roku YouTube app' to control it from here).")
        if self.media["is_live"]:
            raise RuntimeError("You can't skip around in a live stream.")
        self.cast(self.media, max(0.0, min(t, self.media["duration"] - 3)))

    def stop(self):
        try:
            self.key("Home")
        finally:
            self.clear()

    def volume(self, action, value=None):
        if action == "mute":
            self.key("VolumeMute")
        elif action in ("vol_up", "vol_down"):
            self.key("VolumeUp" if action == "vol_up" else "VolumeDown")
        else:
            raise RuntimeError("Use the + and - buttons for Roku volume.")

    def status(self):
        x = self.player_xml()
        state = x.get("state")
        plugin = x.find("plugin")
        pid = plugin.get("id") if plugin is not None else None
        pos = int(re.sub(r"\D", "", x.findtext("position") or "0") or 0) / 1000
        recent = time.time() - self.cast_time < 25   # Roku's YouTube app is slow to start
        ours = MEDIA_ASSISTANT if self.mode == "hls" else ROKU_YOUTUBE
        if self.mode == "youtube" and recent and state in ("close", "stop"):
            return {"position": 0, "state": "loading", "remote": True, "can_seek": False}
        if pid and pid != ours and not recent:
            self.clear()
            return {"state": "stopped", "position": 0}
        out = {"position": self.offset + pos, "volume": None, "muted": None, "remote": True,
               "can_seek": self.mode == "hls" and not (self.media or {}).get("is_live")}
        out["state"] = {"play": "playing", "pause": "paused", "buffer": "loading",
                        "startup": "loading"}.get(state, "loading" if recent else "stopped")
        if out["state"] == "stopped" and not recent:
            self.clear()
        return out


# ---------------------------------------------------------------------------- discovery

def ssdp_search(timeout=3.0):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n').encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.4)
    try:
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        out, end = [], time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            h = {}
            for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
                k, _, v = line.partition(":")
                h[k.strip().lower()] = v.strip()
            out.append((addr[0], h))
        return out
    finally:
        s.close()


def scan():
    replies = ssdp_search()
    rokus, renderers = set(), {}
    for ip, h in replies:
        if "roku" in h.get("server", "").lower() or h.get("st") == "roku:ecp":
            rokus.add(ip)
        elif h.get("location"):
            renderers.setdefault(h["location"], ip)
    for ip in rokus:
        dev = next((d for d in DEVICES.values() if d.kind == "roku" and d.ip == ip), None)
        try:
            if not dev:
                x = ET.fromstring(http_get(f"http://{ip}:8060/query/device-info", timeout=4))
                dev_id = "roku-" + (x.findtext("serial-number") or x.findtext("device-id") or ip)
                dev = DEVICES.get(dev_id) or RokuDevice(dev_id, "Roku", ip)
                dev.ip, dev.base = ip, f"http://{ip}:8060"
                with DEV_LOCK:
                    DEVICES[dev_id] = dev
                log(f"found Roku at {ip}")
            dev.refresh()
            dev.last_seen = time.time()
        except Exception as e:
            log(f"Roku at {ip}: {e}")
    try:
        me = {local_ip_for("192.168.0.1")}
    except Exception:
        me = set()
    for loc, ip in renderers.items():
        if ip in me or ip in rokus:
            continue
        dev = next((d for d in DEVICES.values() if d.kind == "dlna" and d.location == loc), None)
        if dev:
            dev.last_seen = time.time()
            continue
        try:
            desc = describe_renderer(loc)
        except Exception:
            continue
        if not desc:
            continue
        dev_id = "dlna-" + desc["udn"].replace("uuid:", "")
        name = desc["name"]
        if "awox" in (name + desc["manufacturer"]).lower():
            name = "Sanyo TV"   # Sanyo's TVs use AwoX's renderer and a generic name
        name = re.sub(r"\s*\([0-9a-f:]{17}\)$", "", name, flags=re.I)
        dev = DEVICES.get(dev_id)
        if dev:
            dev.ip, dev.location, dev.avt_url, dev.rc_url = ip, loc, desc["avt"], desc["rc"]
        else:
            dev = DlnaDevice(dev_id, name, ip, loc, desc["avt"], desc["rc"])
            with DEV_LOCK:
                DEVICES[dev_id] = dev
            log(f"found {name} at {ip}")
        dev.last_seen = time.time()


def discovery_loop():
    while True:
        try:
            scan()
        except Exception as e:
            log("scan failed:", e)
        time.sleep(10)


def monitor_loop():
    """Keeps an eye on DLNA TVs even when no browser is open (to end videos cleanly)."""
    while True:
        time.sleep(2)
        for d in list(DEVICES.values()):
            if d.kind == "dlna" and d.media and d.mode == "file" and not d.busy:
                try:
                    with d.lock:
                        d.status()
                except Exception:
                    pass


# ============================================================================ API

def device_or_fail(dev_id):
    dev = DEVICES.get(dev_id or CONFIG.get("selected") or "")
    if not dev:
        online = [d for d in DEVICES.values() if d.online]
        if not online:
            raise RuntimeError("No TV found yet. Make sure your TVs are on and on the same Wi-Fi.")
        dev = online[0]
    return dev


def api_state(dev_id):
    devs = sorted(DEVICES.values(), key=lambda d: (not d.online, d.name.lower()))
    sel = DEVICES.get(dev_id or "") or DEVICES.get(CONFIG.get("selected") or "")
    if not sel or (not sel.online and any(d.online for d in devs)):
        sel = next((d for d in devs if d.online), sel)
    player = None
    if sel:
        # A cast or skip can hold the lock for a few seconds; don't freeze the UI meanwhile.
        if sel.lock.acquire(timeout=0.3):
            try:
                player = sel.player()
                sel.last_player = player
            finally:
                sel.lock.release()
        else:
            last = getattr(sel, "last_player", None) or {}
            player = dict(last, busy=sel.busy) if (last and not sel.busy) else {"busy": sel.busy or "Working..."}
    return {"devices": [d.info() for d in devs], "selected": sel and sel.id, "player": player}


def api_cast(data):
    dev = device_or_fail(data.get("device"))
    if dev.busy:
        raise RuntimeError(f"{dev.name} is still loading the last video.")
    dev.busy = "Finding the video..."
    try:
        media = dict(resolve_request(data))   # a copy: resolved items are cached and shared
        # Subtitles the browser extension found on the page come first (they're what the page
        # was showing); then any the stream or site itself lists.
        found = [s for s in (data.get("subtitles") or []) if s.get("url") or s.get("text")][:20]
        media["subtitles"] = found + list(media.get("subtitles") or [])
        media["sub_style"] = dict(SUB_STYLE, **{k: float(v) for k, v in (data.get("sub_style") or {}).items()
                                                 if k in SUB_STYLE})
        sub = data.get("sub")
        if sub is None:
            sub = next((i for i, s in enumerate(found) if s.get("active")), -1)
        media["sub"] = int(sub) if int(sub) < len(media["subtitles"]) else -1
        start = float(data.get("start") or 0)
        if media["is_live"] or start < 5 or (media["duration"] and start > media["duration"] - 10):
            start = 0
        dev.busy = f"Starting on {dev.name}..."
        with dev.lock:
            dev.cast(media, start)
        return {"ok": True, "title": media["title"], "device": dev.name}
    finally:
        dev.busy = None


def resolve_request(data):
    """Work out what to play from a web-page request or a browser-extension request.

    The extension sends the page URL plus the streams it saw the page's player load
    ("candidates", newest first, with the headers and cookies the player used).
    Sites yt-dlp knows (YouTube, Vimeo, Twitch, ...) go through yt-dlp, which picks the best
    quality; anything else uses the stream the page itself was playing.
    """
    page = data.get("url") or data.get("page") or ""
    candidates = data.get("candidates") or []
    if page and (not candidates or ytdlp_supports(page)):
        try:
            return resolve(page)
        except RuntimeError:
            if not candidates:
                raise
    errors, fallback = [], None
    for c in candidates[:8]:
        try:
            media = media_from_stream(c["url"], c.get("headers") or {}, page)
        except Exception as e:
            errors.append(str(e))
            continue
        media["title"] = (data.get("title") or media["title"])[:200]
        media["thumb"] = data.get("thumb") or media["thumb"]
        # Prefer an HLS master playlist: it lists every quality and the separate audio track
        # (a single-quality playlist the player picked may be video-only).
        if media.pop("_master", False) or not media["sources"][0]["protocol"] == "m3u8":
            return media
        fallback = fallback or media
    if fallback:
        return fallback
    if page:
        try:
            return resolve(page)
        except RuntimeError:
            pass
    raise RuntimeError("Couldn't play the video on this page. "
                       + ("It may be protected (DRM), which TVs can't be sent. " if candidates else "")
                       + (errors[0][:150] if errors else ""))


_SPECIFIC_IES = None


def ytdlp_supports(url):
    """True if yt-dlp has a dedicated extractor for this site (not just its generic one)."""
    global _SPECIFIC_IES
    if _SPECIFIC_IES is None:
        _SPECIFIC_IES = [ie for ie in yt_dlp.extractor.gen_extractor_classes()
                         if ie.ie_key() not in ("Generic",) and ie._WORKING]
    try:
        return any(ie.suitable(url) for ie in _SPECIFIC_IES)
    except Exception:
        return False


def hls_attrs(line):
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.partition(":")[2])}


def media_from_stream(url, headers, page):
    """Build a playable item from a raw stream URL (.m3u8 / .mpd / .mp4) seen in the browser."""
    host = urllib.parse.urlparse(page or url).netloc.replace("www.", "")
    base = {"key": url, "title": host, "thumb": None, "site": host, "youtube_id": None, "quality": ""}
    if re.search(r"\.m3u8(\?|$)|/manifest|mpegurl", url, re.I) and ".mpd" not in url.lower():
        txt = http_get(url, headers=headers, timeout=10).decode("utf-8", "replace")
        if "#EXTM3U" not in txt:
            raise RuntimeError("not an HLS playlist")
        video_url, audio_url, height, codecs, subs = url, None, None, "", []
        if "#EXT-X-STREAM-INF" in txt:
            # A master playlist: pick the best quality up to 1080p (H.264 preferred) ourselves.
            lines = txt.splitlines()
            audio = {}
            for ln in lines:
                if ln.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in ln:
                    a = hls_attrs(ln)
                    if a.get("URI") and (a.get("GROUP-ID") not in audio or a.get("DEFAULT") == "YES"):
                        audio[a.get("GROUP-ID")] = urllib.parse.urljoin(url, a["URI"])
                elif ln.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in ln:
                    a = hls_attrs(ln)
                    if a.get("URI"):
                        subs.append({"label": a.get("NAME") or a.get("LANGUAGE") or "Subtitles",
                                     "lang": a.get("LANGUAGE", ""), "headers": headers,
                                     "url": urllib.parse.urljoin(url, a["URI"])})
            variants = []
            for i, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF:"):
                    a = hls_attrs(ln)
                    uri = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
                    if not uri:
                        continue
                    h = int(a["RESOLUTION"].split("x")[1]) if "x" in a.get("RESOLUTION", "") else 0
                    variants.append((h <= 1080, "hvc1" not in a.get("CODECS", "") and "hev1" not in a.get("CODECS", ""),
                                     int(a.get("BANDWIDTH", 0) or 0), h, a, urllib.parse.urljoin(url, uri)))
            if not variants:
                raise RuntimeError("empty HLS playlist")
            _, _, _, height, a, video_url = max(variants, key=lambda v: v[:3])
            audio_url = audio.get(a.get("AUDIO"))
            codecs = a.get("CODECS", "")
            txt = http_get(video_url, headers=headers, timeout=10).decode("utf-8", "replace")
        dur = sum(float(m) for m in re.findall(r"#EXTINF:([\d.]+)", txt)) if "#EXT-X-ENDLIST" in txt else 0.0
        vcodec = "avc1" if "avc1" in codecs else ("hevc" if re.search(r"hvc1|hev1", codecs) else "")
        sources = [{"url": video_url, "headers": headers, "protocol": "m3u8", "vcodec": vcodec,
                    "acodec": "" if audio_url else ("mp4a" if "mp4a" in codecs else "")}]
        if audio_url:
            sources.append({"url": audio_url, "headers": headers, "protocol": "m3u8",
                            "vcodec": "none", "acodec": "mp4a" if "mp4a" in codecs else ""})
        return dict(base, duration=dur, is_live=not dur, sources=sources, subtitles=subs,
                    quality=f"{height}p" if height else "", _master=video_url != url)
    # .mp4 / .webm / .mpd: ask ffprobe what it is
    cmd = ["ffprobe", "-v", "error"]
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    if hdr:
        cmd += ["-headers", hdr]
    cmd += ["-show_entries", "format=duration:stream=codec_type,codec_name,height", "-of", "json", url]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW)
    if out.returncode:
        raise RuntimeError((out.stderr.strip().splitlines() or ["the stream couldn't be opened"])[-1])
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if not v:
        raise RuntimeError("no video in that stream")
    try:
        dur = float((info.get("format") or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    proto = "m3u8" if ".mpd" in url.lower() else "https"   # ffmpeg reads DASH itself
    return dict(base, duration=dur, is_live=not dur,
                quality=f"{v.get('height')}p" if v.get("height") else "",
                sources=[{"url": url, "headers": headers, "protocol": proto,
                          "vcodec": v.get("codec_name", ""), "acodec": a.get("codec_name", "")}])


def api_control(data):
    dev = device_or_fail(data.get("device"))
    action, value = data.get("action"), data.get("value")
    if not dev.lock.acquire(timeout=20):
        raise RuntimeError(f"{dev.name} is busy. Try again in a moment.")
    try:
        return control(dev, action, value)
    finally:
        dev.lock.release()


def control(dev, action, value):
    if action in ("vol_up", "vol_down", "mute", "set_volume"):
        dev.volume(action, value)
        return {"ok": True}
    if not dev.media:
        raise RuntimeError("Nothing is playing.")
    if action in ("subs", "subs_add", "subs_style"):
        if dev.media["is_live"] or dev.mode == "youtube":
            raise RuntimeError("Subtitles work for videos (not live streams or Roku's YouTube app).")
        if action == "subs":
            dev.switch_subtitles(int(value))
        elif action == "subs_add":
            if not parse_cues((value or {}).get("text") or ""):
                raise RuntimeError("That file doesn't look like subtitles (.srt or .vtt).")
            dev.switch_subtitles(-1, extra=value)
        else:
            style = dict(SUB_STYLE, **{k: float(v) for k, v in (value or {}).items() if k in SUB_STYLE})
            if sub_on(dev.media):
                dev.switch_subtitles(dev.media["sub"], style=style)
            else:   # remember it for when subtitles are turned on (a copy: sessions share dicts)
                dev.media = dict(dev.media, sub_style=style)
        return {"ok": True}
    actions = {"toggle": dev.toggle, "play": dev.play, "pause": dev.pause, "stop": dev.stop,
               "seek": lambda: dev.seek(float(value))}
    if action not in actions:
        raise RuntimeError(f"Unknown action {action}")
    actions[action]()
    return {"ok": True}


def api_device(data):
    dev = DEVICES.get(data.get("id") or "")
    if not dev:
        raise RuntimeError("Unknown TV")
    if data.get("select"):
        CONFIG["selected"] = dev.id
    if "name" in data:
        name = (data["name"] or "").strip()[:40]
        if name:
            CONFIG["names"][dev.id] = name
        else:
            CONFIG["names"].pop(dev.id, None)
    if "youtube_app" in data:
        CONFIG["youtube_app"][dev.id] = bool(data["youtube_app"])
    save_config()
    return {"ok": True}


# ============================================================================ HTTP server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"   # TVs prefer plain, unchunked responses

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def not_found(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routing
    def do_HEAD(self):
        self.route(head=True)

    def do_GET(self):
        self.route(head=False)

    def route(self, head):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        parts = u.path.strip("/").split("/")
        try:
            if u.path == "/":
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if not head:
                    self.wfile.write(data)
            elif u.path == "/api/state":
                self.send_json(api_state(q.get("device", [""])[0]))
            elif u.path == "/api/threads" and DEBUG:
                import traceback
                names = {t.ident: t.name for t in threading.enumerate()}
                out = []
                for tid, fr in sys._current_frames().items():
                    stack = traceback.extract_stack(fr)[-6:]
                    out.append(f"--- {names.get(tid, tid)}\n" + "".join(
                        f"  {os.path.basename(f.filename)}:{f.lineno} {f.name}: {f.line}\n" for f in stack))
                data = "\n".join(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/search":
                self.send_json({"results": search(q.get("q", [""])[0])})
            elif parts[0] in ("src", "dlna", "hls") and len(parts) >= 3 and parts[1] in SESSIONS:
                s = SESSIONS[parts[1]]
                if DEBUG and parts[0] == "dlna":
                    log(f"[tv>] {self.command} {parts[2]} Range={self.headers.get('Range')}")
                if parts[0] == "src":
                    self.serve_source(s, int(parts[2]), head)
                elif parts[0] == "dlna":
                    if parts[2].startswith("live-"):
                        self.serve_live(s, int(q.get("from", ["0"])[0]), head)
                    else:
                        self.serve_file(s, head)
                elif parts[2] == "seg":
                    self.serve_segment(s, int(parts[3].split(".")[0]), head)
                else:
                    self.serve_playlist(s, int(q.get("from", ["0"])[0]), head)
            else:
                self.not_found()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            if self.path.startswith("/api/"):
                self.send_json({"error": str(e)}, 500)
            else:
                log("serve error:", self.path, e)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path).path
        # Only this page and the Projecter browser extension may control the TVs; any other
        # website you visit could otherwise send commands to localhost.
        origin = self.headers.get("Origin")
        if origin and not (origin.startswith("chrome-extension://") or re.match(
                rf"^http://(localhost|127\.0\.0\.1|\[::1\]):{PORT}$", origin)):
            return self.send_json({"error": "Not allowed"}, 403)
        try:
            data = self.body()
            if u == "/api/cast":
                return self.send_json(api_cast(data))
            if u == "/api/control":
                return self.send_json(api_control(data))
            if u == "/api/device":
                return self.send_json(api_device(data))
            if u == "/api/scan":
                scan()
                return self.send_json({"ok": True})
            self.not_found()
        except Exception as e:
            log(f"{u} failed:", e)
            self.send_json({"error": str(e)}, 500)

    # ---- chunked fetch proxy (beats YouTube's throttling of long downloads)
    def serve_source(self, s, i, head):
        src = s.media["sources"][i]
        size = src["size"]
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        a = int(m.group(1)) if m else 0
        b = int(m.group(2)) if m and m.group(2) else size - 1
        b = min(b, size - 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        if m:
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.end_headers()
        if head:
            return
        ex = ThreadPoolExecutor(2)
        futs, nxt = deque(), a
        try:
            def submit():
                nonlocal nxt
                if nxt <= b:
                    e = min(b, nxt + CHUNK - 1)
                    futs.append(ex.submit(fetch_range, src["url"], src["headers"], nxt, e))
                    nxt = e + 1
            submit()
            submit()
            while futs and not s.closed:
                data = futs.popleft().result()
                submit()
                self.wfile.write(data)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    # ---- DLNA
    def dlna_headers(self, flags):
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", flags)

    def serve_file(self, s, head):
        vs = s.vsize or s.virtual_size()
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        a = int(m.group(1)) if m else 0
        b = min(int(m.group(2)) if m and m.group(2) else vs - 1, vs - 1)
        if a >= vs:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{vs}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if m else 200)
        self.dlna_headers(FILE_FLAGS)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        if m:
            self.send_header("Content-Range", f"bytes {a}-{b}/{vs}")
        self.end_headers()
        if head:
            return
        start = 0
        if a > 0:
            if a > s.written and not s.done and a > vs * 0.97:
                start = None    # the TV peeking at the very end of the file; don't make it wait
            else:
                try:
                    s.wait(lambda: s.written > a, timeout=120)
                except RuntimeError:
                    return
                # start on a keyframe (segment start) so the picture appears immediately
                start = s.segs[s.seg_for_byte(a)][2] if s.segs and a < s.written else a // 188 * 188
        if start is None:
            left = b - a + 1
            while left > 0:
                n = min(len(NULLS), left)
                self.wfile.write(NULLS[:n])
                left -= n
        else:
            s.pump(self.wfile, start, b - a + 1)

    def serve_live(self, s, k, head):
        self.send_response(200)
        self.dlna_headers(LIVE_FLAGS)
        self.end_headers()
        if not head:
            s.pump(self.wfile, s.segs[min(k, len(s.segs) - 1)][2] if s.segs else 0)

    # ---- Roku (HLS)
    def serve_playlist(self, s, k, head):
        segs = s.segs
        if not segs:
            return self.not_found()
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-TARGETDURATION:{int(max(x[1] for x in segs)) + 1}",
                 "#EXT-X-MEDIA-SEQUENCE:0"]
        # Live streams are announced as a growing recording (EVENT): Roku's player refuses to
        # pause anything marked live, but happily pauses a recording we keep extending.
        lines.append("#EXT-X-PLAYLIST-TYPE:" + ("VOD" if s.done else "EVENT"))
        lines.append("#EXT-X-START:TIME-OFFSET=0,PRECISE=YES")
        for i in range(min(k, len(segs) - 1), len(segs)):
            lines += [f"#EXTINF:{segs[i][1]:.3f},", f"seg/{i}.ts"]
        if s.done:
            lines.append("#EXT-X-ENDLIST")
        data = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def serve_segment(self, s, i, head):
        if i >= len(s.segs):
            return self.not_found()
        _, _, off, ln = s.segs[i]
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(ln))
        self.end_headers()
        if head:
            return
        with open(s.ts, "rb") as f:
            f.seek(off)
            self.wfile.write(f.read(ln))


class Server(ThreadingHTTPServer):
    """Listens on IPv6 and IPv4 at once. On Windows 'localhost' tries IPv6 first, and an
    IPv4-only server costs every browser request a 2-second fallback."""
    daemon_threads = True
    allow_reuse_address = False
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


# ============================================================================ UI

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Projecter</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#07080c; --panel:rgba(22,24,33,.62); --panel-solid:#151720; --line:rgba(255,255,255,.08);
  --line-2:rgba(255,255,255,.14); --text:#f2f3f7; --mute:#9095a6; --dim:#5d6273;
  --a1:#ff3d6e; --a2:#7a5cff; --a3:#22d3ee; --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
  --grad:linear-gradient(120deg,var(--a1),var(--a2) 55%,var(--a3));
  --r:18px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;min-height:100%}
body{background:var(--bg);color:var(--text);font:15px/1.45 Inter,system-ui,Segoe UI,sans-serif;overflow-x:hidden}
button{font:inherit;color:inherit;cursor:pointer;border:0;background:none}
input{font:inherit;color:inherit}
::selection{background:rgba(122,92,255,.45)}

/* ambient backdrop */
.amb{position:fixed;inset:-20%;z-index:0;pointer-events:none;
  background:radial-gradient(40% 40% at 20% 15%,rgba(255,61,110,.22),transparent 70%),
             radial-gradient(45% 45% at 85% 10%,rgba(122,92,255,.22),transparent 70%),
             radial-gradient(50% 50% at 60% 95%,rgba(34,211,238,.14),transparent 70%);
  animation:drift 24s ease-in-out infinite alternate}
@keyframes drift{to{transform:translate3d(3%,2%,0) rotate(6deg)}}
.art{position:fixed;inset:-10%;z-index:0;pointer-events:none;background-size:cover;background-position:center;
  filter:blur(90px) saturate(1.7);opacity:0;transition:opacity 1.2s ease,background-image 1s}
.art.on{opacity:.42}
.grain{position:fixed;inset:0;z-index:1;pointer-events:none;opacity:.06;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

.wrap{position:relative;z-index:2;max-width:1180px;margin:0 auto;padding:22px 20px 80px}
.glass{background:var(--panel);border:1px solid var(--line);backdrop-filter:blur(22px) saturate(1.4);
  -webkit-backdrop-filter:blur(22px) saturate(1.4);border-radius:var(--r)}

/* header */
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:26px}
.logo{display:flex;align-items:center;gap:10px;font:700 22px/1 "Space Grotesk",Inter,sans-serif;letter-spacing:-.5px;margin-right:auto}
.logo .mark{width:34px;height:34px;border-radius:10px;background:var(--grad);display:grid;place-items:center;
  box-shadow:0 8px 30px -6px rgba(255,61,110,.6)}
.logo .mark svg{width:18px;height:18px}
.logo span b{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.devs{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.dev{position:relative;display:flex;align-items:center;gap:10px;padding:8px 14px 8px 10px;border-radius:14px;
  background:var(--panel);border:1px solid var(--line);backdrop-filter:blur(18px);transition:.2s;min-width:0}
.dev:hover{border-color:var(--line-2);transform:translateY(-1px)}
.dev.sel{border-color:transparent;background:linear-gradient(var(--panel-solid),var(--panel-solid)) padding-box,var(--grad) border-box;
  border:1px solid transparent;box-shadow:0 10px 40px -12px rgba(122,92,255,.55)}
.dev.off{opacity:.45}
.dev .ic{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:rgba(255,255,255,.06)}
.dev.roku .ic{background:rgba(122,92,255,.18);color:#b9a8ff}
.dev.dlna .ic{background:rgba(34,211,238,.14);color:#8eeafc}
.dev .ic svg{width:17px;height:17px}
.dev .nm{font-weight:600;font-size:14px;white-space:nowrap;max-width:170px;overflow:hidden;text-overflow:ellipsis}
.dev .st{font-size:11.5px;color:var(--mute);display:flex;align-items:center;gap:6px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.dot.online{background:var(--ok);box-shadow:0 0 0 3px rgba(52,211,153,.18)}
.dot.standby{background:var(--warn)}
.eq{display:inline-flex;gap:2px;align-items:flex-end;height:10px}
.eq i{width:2px;background:var(--a3);border-radius:2px;animation:eq 1s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:-.4s}.eq i:nth-child(3){animation-delay:-.7s}
@keyframes eq{0%,100%{height:3px}50%{height:10px}}
.dev .gear{margin-left:2px;width:24px;height:24px;border-radius:7px;display:grid;place-items:center;color:var(--mute);opacity:.0;transition:.15s}
.dev:hover .gear,.dev.sel .gear{opacity:1}
.dev .gear:hover{background:rgba(255,255,255,.08);color:var(--text)}
.iconbtn{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;color:var(--mute);border:1px solid var(--line);background:var(--panel);backdrop-filter:blur(18px)}
.iconbtn:hover{color:var(--text);border-color:var(--line-2)}
.iconbtn svg{width:17px;height:17px}
.spin svg{animation:rot 1s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}

/* search */
.hero{margin:6px 0 26px}
.hero h1{font:700 clamp(28px,4.4vw,46px)/1.05 "Space Grotesk",Inter,sans-serif;letter-spacing:-1.5px;margin:0 0 6px}
.hero h1 em{font-style:normal;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{margin:0 0 18px;color:var(--mute)}
.bar{display:flex;align-items:center;gap:8px;padding:8px;border-radius:20px;transition:.2s}
.bar:focus-within{border-color:rgba(122,92,255,.6);box-shadow:0 0 0 4px rgba(122,92,255,.15),0 20px 60px -20px rgba(122,92,255,.5)}
.bar .si{width:22px;height:22px;margin-left:10px;color:var(--mute);flex:none}
.bar input{flex:1;min-width:0;background:none;border:0;outline:0;font-size:17px;padding:12px 6px}
.bar input::placeholder{color:var(--dim)}
.btn{display:inline-flex;align-items:center;gap:8px;padding:12px 18px;border-radius:14px;font-weight:600;white-space:nowrap;transition:.15s}
.btn svg{width:16px;height:16px}
.btn.ghost{color:var(--mute);border:1px solid var(--line)}
.btn.ghost:hover{color:var(--text);border-color:var(--line-2)}
.btn.go{background:var(--grad);color:#fff;box-shadow:0 10px 30px -10px rgba(255,61,110,.7)}
.btn.go:hover{filter:brightness(1.1);transform:translateY(-1px)}
.hint{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;font-size:12.5px;color:var(--dim)}
.hint b{color:var(--mute);font-weight:500}

/* warning */
.warn{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;margin-bottom:18px;border-radius:14px;
  background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.25);color:#fde68a;font-size:14px}
.warn svg{width:18px;height:18px;flex:none;margin-top:1px}

/* now playing */
.np{display:none;position:relative;overflow:hidden;padding:20px;margin-bottom:30px;border-radius:24px}
.np.show{display:grid;grid-template-columns:minmax(0,320px) 1fr;gap:24px;animation:rise .45s cubic-bezier(.2,.8,.2,1)}
@keyframes rise{from{opacity:0;transform:translateY(12px) scale(.985)}}
.np .cover{position:relative;border-radius:16px;overflow:hidden;aspect-ratio:16/9;background:#000;box-shadow:0 20px 60px -20px rgba(0,0,0,.8)}
.np .cover img{width:100%;height:100%;object-fit:cover;display:block}
.np .cover .live{position:absolute;left:10px;top:10px;background:var(--a1);color:#fff;font-size:11px;font-weight:800;letter-spacing:.8px;padding:3px 8px;border-radius:6px}
.np .cover .load{position:absolute;inset:0;display:none;place-items:center;background:rgba(0,0,0,.55);backdrop-filter:blur(3px)}
.np.loading .cover .load{display:grid}
.ring{width:44px;height:44px;border-radius:50%;border:3px solid rgba(255,255,255,.15);border-top-color:#fff;animation:rot .8s linear infinite}
.np .info{display:flex;flex-direction:column;min-width:0}
.np .kick{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mute);text-transform:uppercase;letter-spacing:1.2px;font-weight:600}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:99px;background:rgba(255,255,255,.07);font-size:12px;color:var(--text);text-transform:none;letter-spacing:0;font-weight:500}
.chip.ok{background:rgba(52,211,153,.12);color:#a7f3d0}
.np .ttl{font:700 clamp(19px,2.3vw,26px)/1.2 "Space Grotesk",Inter,sans-serif;letter-spacing:-.4px;margin:8px 0 4px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.np .sub{color:var(--mute);font-size:13.5px;min-height:20px}
.np .sub .err{color:var(--bad)}
.seek{margin-top:auto;padding-top:16px}
.track{position:relative;height:22px;cursor:pointer;touch-action:none}
.track .rail,.track .buf,.track .fill{position:absolute;left:0;top:9px;height:5px;border-radius:9px}
.track .rail{right:0;background:rgba(255,255,255,.1)}
.track .buf{background:rgba(255,255,255,.18);transition:width .5s}
.track .fill{background:var(--grad)}
.track .knob{position:absolute;top:4px;width:15px;height:15px;margin-left:-7.5px;border-radius:50%;background:#fff;
  box-shadow:0 0 0 4px rgba(255,255,255,.12),0 4px 14px rgba(0,0,0,.5);transform:scale(.0);transition:transform .15s}
.track:hover .knob,.track.drag .knob{transform:scale(1)}
.track:hover .rail,.track:hover .buf,.track:hover .fill{top:8px;height:7px}
.track .tip{position:absolute;bottom:24px;transform:translateX(-50%);background:#000;padding:3px 7px;border-radius:6px;font-size:12px;
  font-variant-numeric:tabular-nums;opacity:0;pointer-events:none;transition:opacity .15s;white-space:nowrap}
.track:hover .tip{opacity:1}
.track.locked{cursor:default}
.times{display:flex;justify-content:space-between;font-size:12.5px;color:var(--mute);font-variant-numeric:tabular-nums;margin-top:2px}
.ctrls{display:flex;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap}
.cb{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;color:var(--text);background:rgba(255,255,255,.06);transition:.15s;position:relative}
.cb:hover{background:rgba(255,255,255,.12)}
.cb:active{transform:scale(.93)}
.cb svg{width:20px;height:20px}
.cb small{position:absolute;font-size:9px;font-weight:800;bottom:7px}
.cb.big{width:60px;height:60px;border-radius:50%;background:#fff;color:#0b0c10;box-shadow:0 12px 40px -10px rgba(255,255,255,.45)}
.cb.big:hover{background:#fff;transform:scale(1.05)}
.cb.big svg{width:24px;height:24px}
.cb[disabled]{opacity:.3;pointer-events:none}
.sp{flex:1}
.vol{display:flex;align-items:center;gap:4px;padding:4px;border-radius:14px;background:rgba(255,255,255,.04)}
.vol .cb{width:36px;height:36px;background:none}
.vol .cb:hover{background:rgba(255,255,255,.08)}
.vol .lvl{min-width:28px;text-align:center;font-size:13px;color:var(--mute);font-variant-numeric:tabular-nums}
.nudge{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font:700 30px "Space Grotesk";opacity:0;pointer-events:none;
  text-shadow:0 4px 20px rgba(0,0,0,.6)}
.nudge.pop{animation:pop .7s ease}
@keyframes pop{0%{opacity:0;transform:translate(-50%,-50%) scale(.7)}25%{opacity:1;transform:translate(-50%,-50%) scale(1.05)}100%{opacity:0;transform:translate(-50%,-50%) scale(1)}}

/* subtitles */
.subbar{display:flex;align-items:center;gap:10px;width:100%;margin-top:14px;padding:11px 14px;border-radius:14px;
  background:rgba(255,255,255,.06);text-align:left;font-weight:600;font-size:14px;transition:background .15s}
.subbar:hover{background:rgba(255,255,255,.11)}
.subbar[disabled]{opacity:.45;cursor:default}
.ccb{flex:none;padding:2px 7px;border-radius:6px;border:1.5px solid currentColor;font-size:11px;font-weight:800;letter-spacing:.4px}
.subbar.on .ccb{border-color:transparent;background:var(--grad);color:#fff}
#subtxt{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#chev{color:var(--mute)}
.ccp{grid-column:1/-1;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px;padding-top:18px;border-top:1px solid var(--line)}
.ccp[hidden]{display:none}
.cch{font-size:12px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--mute);margin-bottom:10px}
.tracks{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.trk{max-width:100%;padding:7px 14px;border-radius:99px;font-size:13px;font-weight:600;background:rgba(255,255,255,.07);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trk:hover{background:rgba(255,255,255,.13)}
.trk.sel{background:var(--grad);color:#fff}
.drop{display:block;padding:18px;border-radius:14px;border:1.5px dashed rgba(255,255,255,.2);text-align:center;font-size:13.5px;color:var(--mute);cursor:pointer;transition:.15s}
.drop:hover,.drop.over{border-color:var(--a2);background:rgba(122,92,255,.1);color:var(--text)}
.drop u{color:#b9a8ff;text-decoration:none;font-weight:600}
.prev{position:relative;aspect-ratio:16/9;border-radius:12px;overflow:hidden;
  background:linear-gradient(180deg,#5b7fa8 0%,#9fb7c9 45%,#58723f 46%,#3c5230 100%)}
.prev-img{position:absolute;inset:0;background:center/cover;filter:brightness(.8)}
.prev-cap{position:absolute;left:50%;transform:translateX(-50%);max-width:92%;padding:.05em .35em;border-radius:3px;
  font-family:Arial,sans-serif;color:#fff;text-align:center;line-height:1.2;white-space:nowrap}
.sl{display:grid;grid-template-columns:92px 1fr 54px;align-items:center;gap:10px;margin-top:10px;font-size:13px;color:var(--mute)}
.sl input{width:100%;accent-color:#7a5cff}
.sl b{text-align:right;color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.ccfoot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:12px;font-size:12px;color:var(--dim)}
.ccfoot button{color:#b9a8ff;font-weight:600;font-size:13px}
.dropall{position:fixed;inset:0;z-index:60;display:none;place-items:center;background:rgba(7,8,12,.75);backdrop-filter:blur(6px);
  font:700 26px "Space Grotesk",Inter,sans-serif;color:#fff;pointer-events:none}
.dropall.show{display:grid}
.dropall span{padding:30px 44px;border-radius:24px;border:2px dashed var(--a2);background:rgba(122,92,255,.12)}
@media (max-width:760px){.ccp{grid-template-columns:minmax(0,1fr)}}

/* results */
.sec{display:flex;align-items:center;justify-content:space-between;margin:0 2px 14px}
.sec h2{margin:0;font:600 15px Inter;color:var(--mute);letter-spacing:.2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}
.card{cursor:pointer;border-radius:16px;transition:transform .25s cubic-bezier(.2,.8,.2,1);animation:fade .4s both}
@keyframes fade{from{opacity:0;transform:translateY(8px)}}
.card .th{position:relative;border-radius:14px;overflow:hidden;aspect-ratio:16/9;background:#111}
.card img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .5s cubic-bezier(.2,.8,.2,1),filter .3s}
.card:hover img{transform:scale(1.06);filter:brightness(.55)}
.card .dur{position:absolute;right:8px;bottom:8px;background:rgba(0,0,0,.75);font-size:12px;font-weight:600;padding:2px 6px;border-radius:6px}
.card .dur.lv{background:var(--a1)}
.card .ov{position:absolute;inset:0;display:grid;place-items:center;opacity:0;transition:.25s}
.card:hover .ov{opacity:1}
.card .ov span{display:flex;align-items:center;gap:8px;padding:10px 16px;border-radius:99px;background:rgba(255,255,255,.95);color:#0b0c10;font-weight:700;font-size:13px;transform:translateY(8px);transition:.25s}
.card:hover .ov span{transform:none}
.card .ov svg{width:14px;height:14px}
.card.sending .ov{opacity:1;background:rgba(0,0,0,.45)}
.card .t{margin:10px 2px 2px;font-weight:600;font-size:14.5px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .s{margin:0 2px;font-size:12.5px;color:var(--mute)}
.empty{padding:50px 20px;text-align:center;color:var(--dim)}
.empty svg{width:46px;height:46px;opacity:.5;margin-bottom:10px}
.skel{border-radius:14px;aspect-ratio:16/9;background:linear-gradient(100deg,rgba(255,255,255,.04) 30%,rgba(255,255,255,.09) 50%,rgba(255,255,255,.04) 70%);background-size:300% 100%;animation:sh 1.2s infinite}
@keyframes sh{to{background-position:-150% 0}}

/* toast & popover */
.toasts{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:50;display:flex;flex-direction:column;gap:8px;align-items:center;width:min(560px,calc(100% - 32px))}
.toast{padding:12px 16px;border-radius:14px;background:rgba(20,22,30,.92);border:1px solid var(--line-2);backdrop-filter:blur(20px);
  box-shadow:0 20px 50px -10px rgba(0,0,0,.7);font-size:14px;animation:tin .35s cubic-bezier(.2,.8,.2,1);display:flex;gap:10px;align-items:flex-start}
.toast.err{border-color:rgba(248,113,113,.4)}
.toast.err::before{content:"";flex:none;width:8px;height:8px;margin-top:6px;border-radius:50%;background:var(--bad)}
.toast.ok::before{content:"";flex:none;width:8px;height:8px;margin-top:6px;border-radius:50%;background:var(--ok)}
@keyframes tin{from{opacity:0;transform:translateY(10px) scale(.97)}}
.pop{position:fixed;z-index:40;width:290px;padding:16px;border-radius:16px;background:rgba(20,22,30,.96);border:1px solid var(--line-2);
  box-shadow:0 30px 70px -10px rgba(0,0,0,.8);display:none;backdrop-filter:blur(20px)}
.pop.show{display:block;animation:tin .2s}
.pop label{display:block;font-size:12px;color:var(--mute);margin:0 0 6px}
.pop input[type=text]{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line-2);background:rgba(255,255,255,.04);outline:0}
.pop input[type=text]:focus{border-color:var(--a2)}
.tog{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;font-size:13.5px}
.tog small{display:block;color:var(--mute);font-size:12px;margin-top:2px}
.sw{position:relative;width:40px;height:24px;flex:none;border-radius:99px;background:rgba(255,255,255,.14);transition:.2s}
.sw::after{content:"";position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.2s}
.sw.on{background:var(--a2)}.sw.on::after{left:19px}
.pop .row{display:flex;gap:8px;margin-top:14px}
.pop .row .btn{flex:1;justify-content:center;padding:9px}
kbd{font:600 11px Inter;padding:2px 6px;border-radius:5px;background:rgba(255,255,255,.08);border:1px solid var(--line);color:var(--mute)}

.np .info,.np .cover{min-width:0}
@media (max-width:760px){
  .wrap{padding:16px 16px 60px}
  .np.show{grid-template-columns:minmax(0,1fr);padding:14px;gap:16px}
  .hero p{font-size:14px}
  .btn.ghost .lbl,#golbl{display:none}
  .btn.go{padding:12px 14px}
  .hint{display:none}
  .devs{width:100%}
  .dev{flex:1 1 140px}
  .dev .nm{max-width:100%}
  .ctrls{gap:6px}
  .ctrls .sp{flex-basis:100%;height:0}
  .vol{margin-left:auto}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="amb"></div><div class="art" id="art"></div><div class="grain"></div>
<div class="wrap">
  <header>
    <div class="logo"><div class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 4v16l13-8z" fill="#fff"/></svg></div><span>pro<b>jecter</b></span></div>
    <div class="devs" id="devs"></div>
    <button class="iconbtn" id="scan" title="Look for TVs again"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg></button>
  </header>

  <div id="warn"></div>

  <section class="np glass" id="np">
    <div class="cover"><img id="npimg" alt=""><span class="live" id="nplive" hidden>LIVE</span><div class="load"><div class="ring"></div></div><div class="nudge" id="nudge"></div></div>
    <div class="info">
      <div class="kick"><span id="npon">Now playing</span><span class="chip" id="npchip"></span><span class="chip ok" id="npremote" hidden>TV remote works</span></div>
      <div class="ttl" id="npttl"></div>
      <div class="sub" id="npsub"></div>
      <div class="seek">
        <div class="track" id="track"><div class="rail"></div><div class="buf" id="buf"></div><div class="fill" id="fill"></div><div class="knob" id="knob"></div><div class="tip" id="tip"></div></div>
        <div class="times"><span id="tpos">0:00</span><span id="tdur">0:00</span></div>
      </div>
      <div class="ctrls">
        <button class="cb" id="back" title="Back 10s (J or ←)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg><small>10</small></button>
        <button class="cb big" id="pp" title="Play / pause (Space)"></button>
        <button class="cb" id="fwd" title="Forward 10s (L or →)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg><small>10</small></button>
        <button class="cb" id="stop" title="Stop"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg></button>
        <div class="sp"></div>
        <div class="vol">
          <button class="cb" id="vdown" title="Volume down (↓)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/></svg></button>
          <button class="cb" id="mute" title="Mute (M)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg></button>
          <span class="lvl" id="lvl"></span>
          <button class="cb" id="vup" title="Volume up (↑)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
        </div>
      </div>
      <button class="subbar" id="subbar" title="Subtitles: pick a track, add your own .srt, change the look"><span class="ccb">CC</span><span id="subtxt">Subtitles</span><span id="chev">▾</span></button>
    </div>
    <div class="ccp" id="ccp" hidden>
      <div>
        <div class="cch">Subtitles</div>
        <div class="tracks" id="tracks"></div>
        <label class="drop" id="drop"><input type="file" id="subfile" accept=".srt,.vtt,.ass,.ssa,.txt" hidden><span id="droptxt">Drop an .srt file here (or anywhere on this page) or <u>choose one</u></span></label>
        <div class="ccfoot"><button id="subreset">Reset look</button><span>The TV updates when you let go of a slider (a quick reload).</span></div>
      </div>
      <div>
        <div class="prev" id="prev"><div class="prev-img" id="previmg"></div><span class="prev-cap" id="prevcap">This is how your subtitles will look</span></div>
        <div class="sl"><span>Size</span><input type="range" data-k="size" min="12" max="40" step="1"><b data-v="size"></b></div>
        <div class="sl"><span>Background</span><input type="range" data-k="bg" min="0" max="100" step="5"><b data-v="bg"></b></div>
        <div class="sl"><span>Position</span><input type="range" data-k="pos" min="0" max="100" step="1"><b data-v="pos"></b></div>
        <div class="sl"><span>Timing</span><input type="range" data-k="delay" min="-15" max="15" step="0.1"><b data-v="delay"></b></div>
      </div>
    </div>
  </section>
  <div class="dropall" id="dropall"><span>Drop your subtitle file</span></div>

  <section class="hero">
    <h1 id="h1">Put it on the <em>big screen.</em></h1>
    <p id="h1sub">Paste a link from YouTube or almost any video site, or search YouTube.</p>
    <form class="bar glass" id="f" autocomplete="off">
      <svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" placeholder="Search YouTube or paste a link…">
      <button type="button" class="btn ghost" id="paste" title="Paste a link from the clipboard"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="3" width="8" height="4" rx="1"/><path d="M16 5h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/></svg><span class="lbl">Paste</span></button>
      <button class="btn go" id="gobtn"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg><span id="golbl">Play</span></button>
    </form>
    <div class="hint"><span><kbd>/</kbd> <b>search</b></span><span><kbd>Space</kbd> <b>play/pause</b></span><span><kbd>←</kbd><kbd>→</kbd> <b>skip 10s</b></span><span><kbd>↑</kbd><kbd>↓</kbd> <b>volume</b></span><span><kbd>M</kbd> <b>mute</b></span></div>
  </section>

  <div class="sec" id="sec" hidden><h2 id="sech"></h2></div>
  <div class="grid" id="grid"></div>
</div>
<div class="pop" id="pop"></div>
<div class="toasts" id="toasts"></div>

<script>
const $ = s => document.querySelector(s);
const ICON = {
  play:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  pause:'<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1.2"/><rect x="14" y="5" width="4" height="14" rx="1.2"/></svg>',
  tv:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="13" rx="2.5"/><path d="M8 21h8"/></svg>',
  roku:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="13" rx="2.5"/><path d="M8 21h8"/><path d="m10 9 4 2.5-4 2.5z" fill="currentColor"/></svg>',
  gear:'<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg>',
  warn:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/></svg>',
  cast:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg>',
};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = s => { s = Math.max(0, Math.floor(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`; };
const views = n => n >= 1e9 ? (n/1e9).toFixed(1)+'B' : n >= 1e6 ? (n/1e6).toFixed(1)+'M' : n >= 1e3 ? Math.round(n/1e3)+'K' : n || '';
const isUrl = s => /^https?:\/\//i.test(s) || /^[\w-]+(\.[\w-]+)+\/\S*/.test(s);

async function api(path, body) {
  const opt = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const r = await fetch(path, opt); const j = await r.json();
  if (j.error) throw new Error(j.error); return j;
}
function toast(msg, kind = '', ms = 4200) {
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg;
  $('#toasts').appendChild(t); setTimeout(() => { t.style.transition = '.3s'; t.style.opacity = 0; setTimeout(() => t.remove(), 300); }, ms);
}

// ---------------------------------------------------------------- state
let S = {devices:[], selected:null, player:null};
let selected = (() => { try { return localStorage.getItem('dev'); } catch (e) { return null; } })();
let polledAt = 0, devSig = '';
let pending = null, pendingTimer = null;        // accumulated skip target (debounced)
let seekTarget = null, seekUntil = 0;           // hold the bar at the target while the TV catches up
let stateHold = null, stateUntil = 0;           // optimistic play/pause
let lastErr = '';

const dev = () => S.devices.find(d => d.id === S.selected);
const P = () => S.player;

async function poll() {
  try {
    const j = await api('/api/state' + (selected ? '?device=' + encodeURIComponent(selected) : ''));
    S = j; polledAt = performance.now();
    if (!selected && j.selected) selected = j.selected;
    render();
  } catch (e) {}
}
setInterval(poll, 1000); poll();

function select(id) {
  selected = id; try { localStorage.setItem('dev', id); } catch (e) {}
  api('/api/device', {id, select:true}).catch(() => {});
  S.selected = id; S.player = null; devSig = ''; render(); poll();
}

// ---------------------------------------------------------------- devices
function renderDevices() {
  const sig = JSON.stringify([S.devices, S.selected]);
  if (sig === devSig) return; devSig = sig;
  const box = $('#devs'); box.innerHTML = '';
  if (!S.devices.length) { box.innerHTML = '<div class="dev off"><div class="ic">' + ICON.tv + '</div><div><div class="nm">Looking for TVs…</div><div class="st">Make sure they\'re on</div></div></div>'; }
  for (const d of S.devices) {
    const el = document.createElement('button');
    el.className = `dev ${d.kind} ${d.id === S.selected ? 'sel' : ''} ${d.status === 'offline' ? 'off' : ''}`;
    const st = d.playing ? '<span class="eq"><i></i><i></i><i></i></span> Playing'
      : `<span class="dot ${d.status}"></span>${{online:'Ready', standby:'Standby', offline:'Offline'}[d.status]}`;
    el.innerHTML = `<div class="ic">${d.kind === 'roku' ? ICON.roku : ICON.tv}</div><div style="text-align:left;min-width:0"><div class="nm">${esc(d.name)}</div><div class="st">${st}</div></div><span class="gear" title="TV settings">${ICON.gear}</span>`;
    el.onclick = e => { if (e.target.closest('.gear')) return openPop(d, el); select(d.id); };
    box.appendChild(el);
  }
  const d = dev();
  $('#golbl').textContent = d ? 'Play on ' + d.name : 'Play';
  $('#warn').innerHTML = d && d.warning ? `<div class="warn">${ICON.warn}<div>${esc(d.warning)}</div></div>` : '';
}

function openPop(d, anchor) {
  const pop = $('#pop'); const r = anchor.getBoundingClientRect();
  pop.innerHTML = `<label>TV name</label><input type="text" id="pname" value="${esc(d.name)}" maxlength="40">
    ${d.kind === 'roku' ? `<div class="tog"><div>Use Roku's YouTube app<small>Best quality and works without the laptop, but you skip with the Roku remote.</small></div><button class="sw ${d.youtube_app ? 'on' : ''}" id="pyt"></button></div>` : ''}
    <div class="row"><button class="btn ghost" id="pcancel">Cancel</button><button class="btn go" id="psave">Save</button></div>`;
  pop.style.left = Math.min(window.innerWidth - 306, Math.max(16, r.left)) + 'px';
  pop.style.top = (r.bottom + 8) + 'px'; pop.classList.add('show');
  let yt = d.youtube_app;
  const sw = $('#pyt'); if (sw) sw.onclick = () => { yt = !yt; sw.classList.toggle('on', yt); };
  $('#pcancel').onclick = () => pop.classList.remove('show');
  $('#psave').onclick = async () => {
    try { await api('/api/device', {id:d.id, name:$('#pname').value, youtube_app:yt}); pop.classList.remove('show'); devSig = ''; poll(); }
    catch (e) { toast(e.message, 'err'); }
  };
  $('#pname').focus(); $('#pname').select();
}
document.addEventListener('mousedown', e => { if (!e.target.closest('#pop') && !e.target.closest('.gear')) $('#pop').classList.remove('show'); });

$('#scan').onclick = async () => {
  const b = $('#scan'); b.classList.add('spin');
  try { await api('/api/scan', {}); await poll(); toast(S.devices.length ? `Found ${S.devices.filter(d => d.online).length} TV(s)` : 'No TVs found', 'ok', 2200); }
  catch (e) { toast(e.message, 'err'); } finally { b.classList.remove('spin'); }
};

// ---------------------------------------------------------------- now playing
function curPos() {
  const p = P(); if (!p) return 0;
  const now = performance.now();
  if (pending !== null) return pending;
  if (seekTarget !== null && now < seekUntil) {
    if (Math.abs((p.position || 0) - seekTarget) > 4 || p.state !== 'playing') return seekTarget;
    seekTarget = null;
  }
  let pos = p.position || 0;
  if (state() === 'playing') pos += (now - polledAt) / 1000;
  return p.duration ? Math.min(pos, p.duration) : pos;
}
function state() {
  const p = P(); if (!p) return 'stopped';
  if (stateHold && performance.now() < stateUntil) {
    if (p.state === stateHold) stateHold = null; else return stateHold;
  }
  return p.state;
}

let artUrl = '';
function render() {
  renderDevices();
  const p = P(), np = $('#np'), d = dev();
  const show = p && (p.title || p.busy);
  np.classList.toggle('show', !!show);
  const thumb = p && p.thumb || '';
  if (thumb !== artUrl) { artUrl = thumb; $('#art').style.backgroundImage = thumb ? `url("${thumb}")` : ''; $('#art').classList.toggle('on', !!thumb); }
  if (!show) { $('#art').classList.remove('on'); artUrl = ''; return; }
  $('#art').classList.toggle('on', !!thumb);
  if (p.title) {
    if ($('#npimg').getAttribute('src') !== thumb) $('#npimg').src = thumb || 'data:,';
    $('#npttl').textContent = p.title;
  }
  $('#nplive').hidden = !p.live;
  const st = state();
  np.classList.toggle('loading', !!p.busy || st === 'loading');
  $('#npon').textContent = 'On ' + (d ? d.name : 'TV');
  $('#npchip').textContent = p.busy ? p.busy : ({playing:'Playing', paused:'Paused', loading:'Loading…', stopped:'Stopped', ended:'Finished', unknown:'Not responding'}[st] || st);
  $('#npremote').hidden = !p.remote || !!p.busy;
  const bits = [p.site, p.quality, p.mode === 'youtube' ? "in Roku's YouTube app" : ''].filter(Boolean);
  $('#npsub').innerHTML = p.error ? `<span class="err">${esc(p.error)}</span>` : esc(bits.join(' · '));
  $('#pp').innerHTML = st === 'playing' || st === 'loading' ? ICON.pause : ICON.play;
  const canSeek = !!p.can_seek && !p.busy;
  $('#back').disabled = $('#fwd').disabled = !canSeek;
  $('#track').classList.toggle('locked', !canSeek);
  $('#lvl').textContent = p.muted ? 'muted' : (p.volume ?? '');
  if (p.error && p.error !== lastErr) { lastErr = p.error; }
  if (p.notice && p.notice !== lastNotice) { lastNotice = p.notice; toast(p.notice, 'err', 7000); }
  renderSubs(p);
}

// ---------------------------------------------------------------- subtitles
const SUB_DEFAULT = {size: 20, bg: 0, pos: 6, delay: 0};
let subStyle = {...SUB_DEFAULT}, styleTimer = null, styleTouched = 0, lastNotice = '';

function renderSubs(p) {
  const ok = p && p.subs_ok && !p.busy, list = (p && p.subtitles) || [], bar = $('#subbar');
  bar.disabled = !(p && p.subs_ok);
  bar.classList.toggle('on', !!ok && p.sub >= 0);
  $('#subtxt').textContent = !(p && p.subs_ok) ? 'Subtitles aren’t available for this video'
    : p.sub >= 0 ? `Subtitles: ${list[p.sub]}`
    : list.length ? `Subtitles: Off · ${list.length} found on the page` : 'Subtitles: Off · add your own .srt';
  $('#chev').textContent = $('#ccp').hidden ? '▾' : '▴';
  if (!(p && p.subs_ok)) { $('#ccp').hidden = true; return; }
  // follow the TV's actual style, unless you've just moved a slider here
  if (p.sub_style && Date.now() - styleTouched > 15000 && JSON.stringify(p.sub_style) !== JSON.stringify(subStyle)) {
    subStyle = {...SUB_DEFAULT, ...p.sub_style}; syncSliders();
  }
  const tr = $('#tracks'), sig = JSON.stringify([list, p.sub]);
  if (tr.dataset.sig === sig) return;
  tr.dataset.sig = sig;
  tr.innerHTML = [['Off', -1], ...list.map((l, i) => [l, i])]
    .map(([l, i]) => `<button class="trk ${p.sub === i ? 'sel' : ''}" data-i="${i}">${esc(l)}</button>`).join('');
  tr.querySelectorAll('.trk').forEach(b => b.onclick = () => { if (+b.dataset.i !== p.sub) control('subs', +b.dataset.i); });
}

function syncSliders() {
  document.querySelectorAll('.sl input').forEach(i => { i.value = subStyle[i.dataset.k]; });
  preview();
}
function preview() {
  const v = k => document.querySelector(`[data-v="${k}"]`);
  v('size').textContent = subStyle.size;
  v('bg').textContent = subStyle.bg + '%';
  v('pos').textContent = subStyle.pos <= 2 ? 'Bottom' : subStyle.pos >= 95 ? 'Top' : subStyle.pos + '%';
  v('delay').textContent = (subStyle.delay > 0 ? '+' : '') + Number(subStyle.delay).toFixed(1) + 's';
  const H = $('#prev').clientHeight || 200, cap = $('#prevcap');
  cap.style.fontSize = (subStyle.size / 288 * H) + 'px';           // same maths as the TV (288-line canvas)
  cap.style.bottom = ((8 + subStyle.pos / 100 * 230) / 288 * H) + 'px';
  cap.style.background = subStyle.bg > 0 ? `rgba(0,0,0,${subStyle.bg / 100})` : 'transparent';
  cap.style.textShadow = subStyle.bg > 0 ? 'none' : '0 0 2px #000,0 0 2px #000,0 1px 3px #000';
  const p = P(); if (p && p.thumb) $('#previmg').style.backgroundImage = `url("${p.thumb}")`;
}
document.querySelectorAll('.sl input').forEach(i => {
  i.addEventListener('input', () => { subStyle[i.dataset.k] = parseFloat(i.value); styleTouched = Date.now(); preview(); });
  i.addEventListener('change', () => {
    styleTouched = Date.now();
    clearTimeout(styleTimer);   // several quick adjustments = one reload on the TV
    styleTimer = setTimeout(() => control('subs_style', subStyle), 700);
  });
});
$('#subreset').onclick = () => { subStyle = {...SUB_DEFAULT}; styleTouched = Date.now(); syncSliders(); control('subs_style', subStyle); };
$('#subbar').onclick = () => { const c = $('#ccp'); c.hidden = !c.hidden; $('#chev').textContent = c.hidden ? '▾' : '▴'; if (!c.hidden) syncSliders(); };

async function addSubFile(file) {
  if (!file) return;
  const p = P();
  if (!p || !p.subs_ok) return toast('Start a video on a TV first, then add its subtitles.', 'err');
  if (file.size > 3e6) return toast('That file is too big to be subtitles.', 'err');
  const text = await file.text();
  if (!/-->/.test(text) && !/\[Script Info\]/.test(text)) return toast("That doesn't look like a subtitle file (.srt or .vtt).", 'err');
  if ($('#ccp').hidden) $('#subbar').click();
  $('#droptxt').textContent = `Adding ${file.name}…`;
  await control('subs_add', {label: file.name.replace(/\.[^.]+$/, ''), text});
  $('#droptxt').innerHTML = 'Drop an .srt file here (or anywhere on this page) or <u>choose one</u>';
}
$('#subfile').onchange = () => { addSubFile($('#subfile').files[0]); $('#subfile').value = ''; };
// drop a subtitle file anywhere on the page
let dragDepth = 0;
const hasFile = e => [...(e.dataTransfer && e.dataTransfer.types || [])].includes('Files');
window.addEventListener('dragenter', e => { if (!hasFile(e)) return; dragDepth++; if (P() && P().subs_ok) $('#dropall').classList.add('show'); });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#dropall').classList.remove('show'); } });
window.addEventListener('dragover', e => { if (hasFile(e)) e.preventDefault(); });
window.addEventListener('drop', e => {
  if (!hasFile(e)) return;
  e.preventDefault(); dragDepth = 0; $('#dropall').classList.remove('show');
  addSubFile(e.dataTransfer.files[0]);
});

function frame() {
  const p = P();
  if (p && p.title) {
    const dur = p.duration || 0, pos = curPos();
    const pct = dur ? Math.min(100, pos / dur * 100) : 0;
    if (!dragging) { $('#fill').style.width = pct + '%'; $('#knob').style.left = pct + '%'; }
    $('#buf').style.width = Math.min(100, (p.cached || 0) * 100) + '%';
    if (!dragging) $('#tpos').textContent = p.live ? 'LIVE' : fmt(pos);
    $('#tdur').textContent = p.live ? '' : fmt(dur);
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

async function control(action, value) {
  try { await api('/api/control', {device:S.selected, action, value}); setTimeout(poll, 250); }
  catch (e) { toast(e.message, 'err'); poll(); }
}
function togglePlay() {
  const p = P(); if (!p || !p.title) return;
  const next = state() === 'playing' ? 'paused' : 'playing';
  stateHold = next; stateUntil = performance.now() + 4000; render();
  control(next === 'paused' ? 'pause' : 'play');
}
let rawBase = 0;   // where the bar was when this burst of skips started
function nudge(delta) {
  const p = P(); if (!p || !p.can_seek || p.busy) return;
  if (pending === null) rawBase = curPos();
  pending = Math.max(0, Math.min((p.duration || 0) - 3, (pending ?? rawBase) + delta));
  const diff = Math.round(pending - rawBase);
  const n = $('#nudge'); n.textContent = (diff >= 0 ? '+' : '−') + Math.abs(diff) + 's';
  n.classList.remove('pop'); void n.offsetWidth; n.classList.add('pop');
  clearTimeout(pendingTimer); pendingTimer = setTimeout(commitSeek, 550);
}
function commitSeek() {
  if (pending === null) return;
  seekTarget = pending; seekUntil = performance.now() + 15000; pending = null;
  control('seek', seekTarget);
}
function seekTo(t) { pending = null; seekTarget = t; seekUntil = performance.now() + 15000; control('seek', t); }

$('#pp').onclick = togglePlay;
$('#back').onclick = () => nudge(-10);
$('#fwd').onclick = () => nudge(10);
$('#stop').onclick = () => { control('stop'); S.player = null; render(); };
$('#vup').onclick = () => control('vol_up');
$('#vdown').onclick = () => control('vol_down');
$('#mute').onclick = () => control('mute');

// seek bar
let dragging = false;
const track = $('#track');
const tAt = e => { const r = track.getBoundingClientRect(); const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); return x * ((P() && P().duration) || 0); };
track.addEventListener('pointermove', e => {
  const t = tAt(e); const r = track.getBoundingClientRect();
  $('#tip').style.left = Math.min(r.width, Math.max(0, e.clientX - r.left)) + 'px'; $('#tip').textContent = fmt(t);
  if (dragging) { const pct = t / (P().duration || 1) * 100; $('#fill').style.width = pct + '%'; $('#knob').style.left = pct + '%'; $('#tpos').textContent = fmt(t); }
});
track.addEventListener('pointerdown', e => {
  const p = P(); if (!p || !p.can_seek || p.busy) return;
  dragging = true; track.classList.add('drag'); track.setPointerCapture(e.pointerId);
  const pct = tAt(e) / (p.duration || 1) * 100; $('#fill').style.width = pct + '%'; $('#knob').style.left = pct + '%';
});
track.addEventListener('pointerup', e => {
  if (!dragging) return; dragging = false; track.classList.remove('drag'); seekTo(tAt(e));
});

// ---------------------------------------------------------------- casting & search
async function cast(url, meta = {}, card) {
  const d = dev();
  if (!d) return toast('No TV selected yet. Turn a TV on and press the refresh button.', 'err');
  if (d.status === 'offline') return toast(`${d.name} is offline. Is it turned on?`, 'err');
  if (card) card.classList.add('sending');
  S.player = {title: meta.title || 'Loading…', thumb: meta.thumb || '', busy: 'Finding the video…', position: 0, duration: meta.duration || 0, site: ''};
  seekTarget = null; pending = null; render();
  try {
    await api('/api/cast', {url, device:d.id});
    toast(`Playing on ${d.name}`, 'ok', 2200);
  } catch (e) { toast(e.message, 'err', 7000); }
  finally { if (card) card.classList.remove('sending'); poll(); }
}

function skeleton() {
  $('#grid').innerHTML = Array.from({length:8}, () => '<div><div class="skel"></div><div class="skel" style="aspect-ratio:auto;height:14px;margin-top:12px;width:85%"></div></div>').join('');
}
async function doSearch(q) {
  $('#sec').hidden = false; $('#sech').textContent = `Results for “${q}”`; skeleton();
  try {
    const {results} = await api('/api/search?q=' + encodeURIComponent(q));
    const g = $('#grid'); g.innerHTML = '';
    if (!results.length) g.innerHTML = '<div class="empty">Nothing found.</div>';
    results.forEach((r, i) => {
      const c = document.createElement('div'); c.className = 'card'; c.style.animationDelay = (i * 30) + 'ms';
      const d = dev();
      c.innerHTML = `<div class="th"><img loading="lazy" alt=""><span class="dur ${r.live ? 'lv' : ''}">${r.live ? 'LIVE' : r.duration ? fmt(r.duration) : ''}</span>
        <div class="ov"><span>${ICON.cast}<b class="lbl">Play on ${esc(d ? d.name : 'TV')}</b></span></div></div>
        <div class="t"></div><div class="s"></div>`;
      c.querySelector('img').src = r.thumb; c.querySelector('.t').textContent = r.title;
      c.querySelector('.s').textContent = [r.channel, r.views ? views(r.views) + ' views' : ''].filter(Boolean).join(' · ');
      if (!r.duration && !r.live) c.querySelector('.dur').remove();
      c.onmouseenter = () => { const d = dev(); c.querySelector('.lbl').textContent = 'Play on ' + (d ? d.name : 'TV'); };
      c.onclick = () => cast(r.url, r, c);
      g.appendChild(c);
    });
  } catch (e) { $('#grid').innerHTML = ''; toast(e.message, 'err'); }
}

$('#f').onsubmit = e => {
  e.preventDefault(); const q = $('#q').value.trim(); if (!q) return $('#q').focus();
  if (isUrl(q)) { cast(q); $('#q').select(); } else doSearch(q);
};
$('#paste').onclick = async () => {
  try { const t = (await navigator.clipboard.readText()).trim(); if (!t) return toast('Clipboard is empty', 'err');
    $('#q').value = t; if (isUrl(t)) cast(t); else doSearch(t); }
  catch (e) { toast('Press Ctrl+V in the box instead (the browser blocked clipboard access).', 'err'); $('#q').focus(); }
};

// keyboard
document.addEventListener('keydown', e => {
  if (e.target.matches('input')) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === '/') { e.preventDefault(); $('#q').focus(); $('#q').select(); }
  else if (k === ' ' || k === 'k') { e.preventDefault(); togglePlay(); }
  else if (k === 'ArrowLeft' || k === 'j') { e.preventDefault(); nudge(-10); }
  else if (k === 'ArrowRight' || k === 'l') { e.preventDefault(); nudge(10); }
  else if (k === 'ArrowUp') { e.preventDefault(); control('vol_up'); }
  else if (k === 'ArrowDown') { e.preventDefault(); control('vol_down'); }
  else if (k === 'm') control('mute');
});
</script></body></html>
"""


# ============================================================================ main

def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    try:
        server = Server(("::", PORT), Handler)
    except OSError:
        log(f"Projecter is already running. Open http://localhost:{PORT}")
        if "--no-browser" not in sys.argv:
            webbrowser.open(f"http://localhost:{PORT}")
        return
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    url = f"http://localhost:{PORT}"
    log(f"Projecter is running: {url}   (close this window to quit)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in list(SESSIONS.values()):
            s.close()


if __name__ == "__main__":
    main()
