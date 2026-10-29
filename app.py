import os, re, shutil, tempfile
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, send_from_directory, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp
from yt_dlp.utils import match_filter_func

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
limiter = Limiter(get_remote_address, app=app, default_limits=["300/hour"], storage_uri="memory://")

ALLOWED = ("youtube.com", "youtu.be", "tiktok.com", "facebook.com", "fb.watch",
           "instagram.com", "twitter.com", "x.com", "vimeo.com", "dailymotion.com")
MAX_SECONDS = int(os.getenv("MAX_SECONDS", 1800))      # longest video allowed
MAX_BYTES = int(os.getenv("MAX_MB", 500)) * 1024 * 1024  # largest file allowed
BASE = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20}


def allowed(url):
    try:
        u = urlparse(url)
        h = (u.hostname or "").lower()
    except Exception:
        return False
    return u.scheme in ("http", "https") and any(h == d or h.endswith("." + d) for d in ALLOWED)


@app.get("/")
def index():
    for d in ("static", "."):
        if os.path.exists(os.path.join(app.root_path, d, "index.html")):
            return send_from_directory(os.path.join(app.root_path, d), "index.html")
    abort(404)


@app.post("/api/info")
@limiter.limit("20/minute")
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not allowed(url):
        return jsonify(error="unsupported"), 400
    try:
        with yt_dlp.YoutubeDL(BASE) as y:
            d = y.extract_info(url, download=False)
    except Exception:
        return jsonify(error="fetch_failed"), 422
    if (d.get("duration") or 0) > MAX_SECONDS:
        return jsonify(error="too_long"), 413
    hs = sorted({f["height"] for f in d.get("formats", [])
                 if f.get("height") and f.get("vcodec") != "none"}, reverse=True)
    hs = [h for h in hs if h in (2160, 1440, 1080, 720, 480, 360)] or hs[:3]
    return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"), heights=hs)


@app.get("/api/download")
@limiter.limit("6/minute")
def download():
    url = request.args.get("url", "").strip()
    q = request.args.get("q", "best")
    if not allowed(url) or not re.fullmatch(r"best|audio|\d{3,4}", q):
        abort(400)
    tmp = tempfile.mkdtemp()
    opts = dict(BASE, outtmpl=os.path.join(tmp, "%(id)s.%(ext)s"), max_filesize=MAX_BYTES,
                merge_output_format="mp4",
                match_filter=match_filter_func(f"duration<={MAX_SECONDS}"))
    if q == "audio":
        opts.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
    elif q == "best":
        opts["format"] = "bv*+ba/b"
    else:
        opts["format"] = f"bv*[height<={q}]+ba/b[height<={q}]/b"
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            d = y.extract_info(url, download=True)
        files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl"))]
        path = os.path.join(tmp, files[0])
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        abort(422)
    name = re.sub(r"[^\w\- ]+", "", d.get("title") or "")[:80].strip() or "foger-download"
    resp = send_file(path, as_attachment=True, download_name=name + os.path.splitext(path)[1])
    resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
    return resp
