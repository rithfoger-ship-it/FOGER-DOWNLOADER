
import os
import re
import shutil
import tempfile
import logging
import zipfile

from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp

# --------------------------------------------------
# FOGER DOWNLOADER - Flask Backend
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["300 per hour"],
    storage_uri="memory://"
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
MAX_ITEMS = 20

BASE_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 30,
    "retries": 2,
    "fragment_retries": 2,
    "ignoreerrors": False,
    "noplaylist": False,
    "playlistend": MAX_ITEMS,
    "max_downloads": MAX_ITEMS,
}


# --------------------------------------------------
# URL VALIDATION
# --------------------------------------------------

def is_allowed_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if parsed.scheme not in ("http", "https"):
            return False

        if not host or parsed.username or parsed.password:
            return False

        return any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )

    except Exception:
        return False


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def safe_title(value):
    value = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:100] or "foger-download"


def media_kind(info):
    """Estimate the type from yt-dlp's extracted information."""
    if info.get("entries") is not None:
        return "slideshow"

    formats = info.get("formats") or []
    has_video = any(
        f.get("vcodec") not in (None, "none")
        for f in formats
    )
    has_audio = any(
        f.get("acodec") not in (None, "none")
        for f in formats
    )

    ext = (info.get("ext") or "").lower()

    if ext in ("jpg", "jpeg", "png", "webp", "gif", "avif"):
        return "picture"

    if has_video:
        return "video"

    if has_audio:
        return "audio"

    if info.get("thumbnails") and not has_video:
        return "picture"

    return "unknown"


def public_error(error):
    """Return a useful but limited error message to the frontend."""
    message = str(error).lower()

    if "private" in message or "login" in message or "sign in" in message:
        return "This post may be private or require login."

    if "unsupported url" in message:
        return "This URL format is not supported by the installed yt-dlp version."

    if "ffmpeg" in message:
        return "FFmpeg is missing or failed. Install FFmpeg on the server."

    if "timed out" in message or "timeout" in message:
        return "The source timed out. Please try again."

    if "not available" in message or "removed" in message:
        return "The post may have been removed or is unavailable."

    return "Could not extract this media. Check the server logs for details."


def inspect_media(url):
    options = dict(BASE_OPTIONS)
    options["skip_download"] = True

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.get("/")
def index():
    # Put your website's index.html inside the static folder.
    return app.send_static_file("index.html")


@app.get("/api/health")
def health():
    return jsonify(
        ok=True,
        service="FOGER DOWNLOADER",
        status="running"
    )


# --------------------------------------------------
# MEDIA INFORMATION
# --------------------------------------------------

@app.post("/api/info")
@limiter.limit("20 per minute")
def info():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not is_allowed_url(url):
        return jsonify(
            error="unsupported",
            message="Please provide a supported social-media URL."
        ), 400

    try:
        extracted = inspect_media(url)

        # Playlist/carousel information
        entries = extracted.get("entries")

        if entries is not None:
            items = []

            for entry in entries:
                if not entry:
                    continue

                items.append({
                    "title": entry.get("title") or "Media item",
                    "thumbnail": entry.get("thumbnail"),
                    "type": media_kind(entry),
                })

                if len(items) >= MAX_ITEMS:
                    break

            if not items:
                return jsonify(
                    error="no_media",
                    message="No downloadable media items were found."
                ), 422

            return jsonify(
                title=extracted.get("title") or "Slideshow",
                thumbnail=extracted.get("thumbnail"),
                type="slideshow",
                count=len(items),
                items=items
            )

        duration = extracted.get("duration")

        if duration and duration > MAX_SECONDS:
            return jsonify(
                error="too_long",
                message=f"Videos must be {MAX_SECONDS // 60} minutes or shorter."
            ), 413

        formats = extracted.get("formats") or []

        heights = sorted({
            f["height"]
            for f in formats
            if f.get("height")
            and f.get("vcodec") not in (None, "none")
        }, reverse=True)

        allowed_heights = [2160, 1440, 1080, 720, 480, 360]
        heights = [
            height for height in allowed_heights
            if height in heights
        ] or heights[:5]

        return jsonify(
            title=extracted.get("title") or "Media",
            thumbnail=extracted.get("thumbnail"),
            type=media_kind(extracted),
            duration=duration,
            heights=heights
        )

    except Exception as error:
        app.logger.exception("Media information extraction failed")

        return jsonify(
            error="fetch_failed",
            message=public_error(error)
        ), 422


# --------------------------------------------------
# DOWNLOAD VIDEO, PICTURE, AUDIO OR SLIDESHOW
# --------------------------------------------------

@app.get("/api/download")
@limiter.limit("6 per minute")
def download():
    url = request.args.get("url", "").strip()
    quality = request.args.get("q", "best").strip().lower()

    if not is_allowed_url(url):
        return jsonify(
            error="unsupported",
            message="Unsupported or invalid URL."
        ), 400

    if not re.fullmatch(r"best|audio|\d{3,4}", quality):
        return jsonify(
            error="bad_quality",
            message="Invalid download quality."
        ), 400

    temp_dir = tempfile.mkdtemp(prefix="foger_")

    options = dict(BASE_OPTIONS)

    # Keep output filenames safe and distinguish carousel items.
    options.update({
        "outtmpl": os.path.join(
            temp_dir,
            "%(playlist_index)03d-%(id)s.%(ext)s"
        ),
        "max_filesize": MAX_BYTES,
        "overwrites": True,
    })

    if quality == "audio":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    elif quality == "best":
        # Prefer best video and audio; fall back to one combined format.
        options["format"] = "bv*+ba/best"

    else:
        options["format"] = (
            f"bv*[height<={quality}]+ba/"
            f"b[height<={quality}]/best"
        )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            extracted = ydl.extract_info(url, download=True)

        # Only include actual media files, not temporary or metadata files.
        excluded = (
            ".part", ".ytdl", ".json", ".description",
            ".vtt", ".srt", ".ass", ".lrc", ".tmp"
        )

        media_files = []

        for filename in os.listdir(temp_dir):
            full_path = os.path.join(temp_dir, filename)

            if not os.path.isfile(full_path):
                continue

            if filename.endswith(excluded):
                continue

            if filename.lower().endswith(".zip"):
                continue

            media_files.append(full_path)

        if not media_files:
            raise RuntimeError(
                "yt-dlp completed without producing a media file."
            )

        media_files.sort()

        title = safe_title(extracted.get("title"))

        if len(media_files) == 1:
            output_path = media_files[0]
            extension = os.path.splitext(output_path)[1]
            download_name = title + extension

        else:
            # A carousel/slideshow with several downloaded items becomes ZIP.
            output_path = os.path.join(temp_dir, title + ".zip")

            with zipfile.ZipFile(
                output_path,
                "w",
                compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for number, file_path in enumerate(media_files, start=1):
                    filename = os.path.basename(file_path)
                    archive.write(
                        file_path,
                        arcname=f"{number:03d}-{filename}"
                    )

            download_name = title + ".zip"

        response = send_file(
            output_path,
            as_attachment=True,
            download_name=download_name
        )

        # Remove temporary files after the response is closed.
        response.call_on_close(
            lambda folder=temp_dir: shutil.rmtree(
                folder,
                ignore_errors=True
            )
        )

        return response

    except Exception as error:
        app.logger.exception("Media download failed")
        shutil.rmtree(temp_dir, ignore_errors=True)

        return jsonify(
            error="download_failed",
            message=public_error(error)
        ), 422


# --------------------------------------------------
# START SERVER
# --------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
