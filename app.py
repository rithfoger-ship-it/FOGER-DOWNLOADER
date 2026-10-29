
import os
import re
import shutil
import tempfile
import zipfile
from urllib.parse import urlparse

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    send_from_directory,
    abort,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp


# --------------------------------------------------
# FOGER DOWNLOADER - Flask backend
# --------------------------------------------------

app = Flask(__name__, static_folder="static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["300 per hour"],
    storage_uri="memory://",
)

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "twitter.com",
    "x.com",
    "vimeo.com",
    "dailymotion.com",
)

MAX_SECONDS = int(os.getenv("MAX_SECONDS", "1800"))
MAX_MB = int(os.getenv("MAX_MB", "500"))
MAX_BYTES = MAX_MB * 1024 * 1024

BASE_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 25,
    "retries": 2,
}


# --------------------------------------------------
# CHECK URL
# --------------------------------------------------

def is_allowed_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if parsed.scheme not in ("http", "https"):
            return False

        if parsed.username or parsed.password:
            return False

        return any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )
    except Exception:
        return False


# --------------------------------------------------
# HOMEPAGE
# index.html is in the repository root
# --------------------------------------------------

@app.get("/")
def index():
    file_path = os.path.join(app.root_path, "index.html")

    if not os.path.isfile(file_path):
        return (
            "Homepage not found. Please check that index.html "
            "is in the repository root.",
            404,
        )

    return send_from_directory(app.root_path, "index.html")


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/api/health")
def health():
    return jsonify(
        status="ok",
        app="FOGER DOWNLOADER",
    )


# --------------------------------------------------
# VIDEO INFORMATION
# --------------------------------------------------

@app.post("/api/info")
@limiter.limit("20 per minute")
def info():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not is_allowed_url(url):
        return jsonify(
            error="Please enter a supported public media URL."
        ), 400

    try:
        options = dict(BASE_OPTIONS)

        with yt_dlp.YoutubeDL(options) as downloader:
            media = downloader.extract_info(
                url,
                download=False,
            )

        if not media:
            return jsonify(error="Could not read this link."), 422

        duration = media.get("duration") or 0

        if duration > MAX_SECONDS:
            return jsonify(
                error="This media is longer than the allowed duration."
            ), 413

        formats = media.get("formats") or []

        heights = sorted(
            {
                int(item["height"])
                for item in formats
                if item.get("height")
                and item.get("vcodec") != "none"
            },
            reverse=True,
        )

        common_heights = [
            height for height in (2160, 1440, 1080, 720, 480, 360)
            if height in heights
        ]

        if common_heights:
            heights = common_heights
        else:
            heights = heights[:5]

        return jsonify(
            title=media.get("title") or "Media",
            thumbnail=media.get("thumbnail"),
            duration=duration,
            heights=heights,
            type=(
                "video" if heights
                else "audio" if formats
                else "unknown"
            ),
        )

    except Exception as error:
        app.logger.warning("Media information error: %s", error)

        return jsonify(
            error=(
                "Could not read this link. It may be private, "
                "removed, or unsupported."
            )
        ), 422


# --------------------------------------------------
# DOWNLOAD MEDIA
# q=best, q=audio, or q=360/480/720/1080/1440/2160
# --------------------------------------------------

@app.get("/api/download")
@limiter.limit("6 per minute")
def download():
    url = request.args.get("url", "").strip()
    quality = request.args.get("q", "best").strip()

    if not is_allowed_url(url):
        abort(400, description="Unsupported URL.")

    if not re.fullmatch(r"best|audio|\d{3,4}", quality):
        abort(400, description="Invalid quality.")

    temp_dir = tempfile.mkdtemp(prefix="foger_")

    try:
        options = dict(BASE_OPTIONS)

        options.update({
            "outtmpl": os.path.join(
                temp_dir,
                "%(title).100s-%(id)s.%(ext)s",
            ),
            "max_filesize": MAX_BYTES,
            "noplaylist": True,
            "overwrites": True,
        })

        if quality == "audio":
            options.update({
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            })

        elif quality == "best":
            options["format"] = "bestvideo*+bestaudio/best"

        else:
            options["format"] = (
                f"bestvideo*[height<={quality}]"
                f"+bestaudio/best[height<={quality}]/best"
            )

        with yt_dlp.YoutubeDL(options) as downloader:
            media = downloader.extract_info(
                url,
                download=True,
            )

        files = [
            name for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
            and not name.endswith((".part", ".ytdl", ".json"))
        ]

        if not files:
            raise RuntimeError("No downloadable media file was created.")

        # Select the largest output file to avoid selecting
        # a small metadata or thumbnail file by mistake.
        files.sort(
            key=lambda name: os.path.getsize(
                os.path.join(temp_dir, name)
            ),
            reverse=True,
        )

        media_file = os.path.join(temp_dir, files[0])

        safe_title = re.sub(
            r'[^\w .-]',
            "",
            media.get("title") or "foger-download",
        ).strip()

        safe_title = safe_title[:80] or "foger-download"

        response = send_file(
            media_file,
            as_attachment=True,
            download_name=safe_title + os.path.splitext(media_file)[1],
        )

        response.call_on_close(
            lambda: shutil.rmtree(temp_dir, ignore_errors=True)
        )

        return response

    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)

        app.logger.warning("Download error: %s", error)

        return jsonify(
            error=(
                "Download failed. The link may be unsupported, "
                "private, unavailable, or too large."
            )
        ), 422


# --------------------------------------------------
# RUN LOCALLY
# Render uses Gunicorn from the Dockerfile.
# --------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
