import ipaddress
import os
import re
import shutil
import socket
import tempfile
from urllib.parse import urlparse, unquote
from urllib.request import url2pathname

import requests
import yt_dlp
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["300 per hour"],
    storage_uri="memory://",
)

MAX_SECONDS = int(os.getenv("MAX_SECONDS", "1800"))
MAX_MB = int(os.getenv("MAX_MB", "500"))
MAX_BYTES = MAX_MB * 1024 * 1024
IMAGE_MAX_MB = int(os.getenv("IMAGE_MAX_MB", "20"))
IMAGE_MAX_BYTES = IMAGE_MAX_MB * 1024 * 1024

ALLOWED_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "facebook.com",
    "fb.watch", "instagram.com", "twitter.com", "x.com",
    "vimeo.com", "dailymotion.com",
    "pinterest.com", "pin.it",
)

IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

BASE_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 25,
    "retries": 2,
}


def is_allowed_social_url(url):
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


def validate_public_image_url(url):
    """Only allow public Internet hosts for direct-image downloads."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            return False
        if parsed.username or parsed.password:
            return False
        if host.lower() in ("localhost", "localhost.localdomain"):
            return False

        addresses = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM
        )
        if not addresses:
            return False

        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def safe_filename(name, fallback="foger-download"):
    name = re.sub(r"[^\w .-]", "", str(name or fallback))
    name = name.strip(" .")[:80]
    return name or fallback


def get_media_filename(path, title):
    extension = os.path.splitext(path)[1] or ".bin"
    return safe_filename(title) + extension


@app.get("/")
def index():
    return send_from_directory(app.root_path, "index.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok", app="FOGER DOWNLOADER")


@app.post("/api/info")
@limiter.limit("20 per minute")
def info():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not is_allowed_social_url(url):
        return jsonify(error="Please enter a supported public video link."), 400

    try:
        with yt_dlp.YoutubeDL(BASE_OPTIONS) as downloader:
            media = downloader.extract_info(url, download=False)

        if not media:
            return jsonify(error="Could not read this link."), 422

        duration = media.get("duration") or 0
        if duration > MAX_SECONDS:
            return jsonify(error="This video is longer than the allowed duration."), 413

        heights = sorted({
            int(item["height"])
            for item in (media.get("formats") or [])
            if item.get("height") and item.get("vcodec") != "none"
        }, reverse=True)

        preferred = [h for h in (2160, 1440, 1080, 720, 480, 360) if h in heights]
        heights = preferred or heights[:5]

        return jsonify(
            title=media.get("title") or "Media",
            thumbnail=media.get("thumbnail"),
            duration=duration,
            heights=heights,
            type="video" if heights else "unknown",
        )
    except Exception as error:
        app.logger.warning("Info error: %s", error)
        return jsonify(
            error="Could not read this link. It may be private, removed, or unsupported."
        ), 422


@app.get("/api/download")
@limiter.limit("6 per minute")
def download_video():
    url = request.args.get("url", "").strip()
    quality = request.args.get("q", "best").strip()

    if not is_allowed_social_url(url):
        return jsonify(error="Unsupported video URL."), 400

    if not re.fullmatch(r"best|audio|\d{3,4}", quality):
        return jsonify(error="Invalid quality."), 400

    temp_dir = tempfile.mkdtemp(prefix="foger_video_")

    try:
        options = dict(BASE_OPTIONS)
        options.update({
            "outtmpl": os.path.join(temp_dir, "%(title).100s-%(id)s.%(ext)s"),
            "max_filesize": MAX_BYTES,
            "noplaylist": True,
            "overwrites": True,
        })

        if quality == "audio":
            options.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        elif quality == "best":
            options["format"] = "bestvideo*+bestaudio/best"
        else:
            options["format"] = (
                f"bestvideo*[height<={quality}]"
                f"+bestaudio/best[height<={quality}]/best"
            )

        with yt_dlp.YoutubeDL(options) as downloader:
            media = downloader.extract_info(url, download=True)

        files = [
            name for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
            and not name.endswith((".part", ".ytdl", ".json"))
        ]

        if not files:
            raise RuntimeError("No downloadable media file was created.")

        files.sort(
            key=lambda name: os.path.getsize(os.path.join(temp_dir, name)),
            reverse=True,
        )
        media_path = os.path.join(temp_dir, files[0])

        response = send_file(
            media_path,
            as_attachment=True,
            download_name=get_media_filename(
                media_path, media.get("title", "foger-video")
            ),
        )
        response.call_on_close(
            lambda: shutil.rmtree(temp_dir, ignore_errors=True)
        )
        return response

    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.warning("Video download error: %s", error)
        return jsonify(
            error="Video download failed. The link may be private, unavailable, unsupported, or too large."
        ), 422


def download_direct_image(url, temp_dir):
    """Fetch a direct image URL while rejecting private hosts and unsafe redirects."""
    current_url = url

    for _ in range(4):
        if not validate_public_image_url(current_url):
            raise ValueError("This image URL is not allowed.")

        with requests.get(
            current_url,
            stream=True,
            timeout=(10, 30),
            allow_redirects=False,
            headers={"User-Agent": "FOGER-DOWNLOADER/1.0"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Invalid image redirect.")
                from urllib.parse import urljoin
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()
            content_type = response.headers.get(
                "Content-Type", ""
            ).split(";")[0].lower()

            if content_type not in IMAGE_TYPES:
                raise ValueError("The link did not return a supported image.")

            length = response.headers.get("Content-Length")
            if length and int(length) > IMAGE_MAX_BYTES:
                raise ValueError("Image exceeds the size limit.")

            path = urlparse(current_url).path
            name = os.path.basename(unquote(path)) or "image"
            name = safe_filename(name, "image")
            extension = IMAGE_TYPES[content_type]
            if not name.lower().endswith(extension):
                name += extension

            file_path = os.path.join(temp_dir, name)
            total = 0
            with open(file_path, "wb") as output:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > IMAGE_MAX_BYTES:
                        raise ValueError("Image exceeds the size limit.")
                    output.write(chunk)

            if total == 0:
                raise ValueError("The image file is empty.")

            return file_path

    raise ValueError("Too many image redirects.")


@app.post("/api/image-download")
@limiter.limit("10 per minute")
def download_image():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    mode = str(data.get("source", "direct")).strip().lower()

    temp_dir = tempfile.mkdtemp(prefix="foger_image_")

    try:
        if mode == "direct":
            image_path = download_direct_image(url, temp_dir)
            response = send_file(
                image_path,
                as_attachment=True,
                download_name=os.path.basename(image_path),
            )
            response.call_on_close(
                lambda: shutil.rmtree(temp_dir, ignore_errors=True)
            )
            return response

        if mode != "social" or not is_allowed_social_url(url):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify(error="Please enter a supported public image or post URL."), 400

        # Social post support is best-effort. Some platforms expose only
        # thumbnails, video, or no image data without authentication.
        with yt_dlp.YoutubeDL(BASE_OPTIONS) as downloader:
            media = downloader.extract_info(url, download=False)

        image_url = media.get("thumbnail") if media else None
        if not image_url:
            raise ValueError(
                "No public image preview was found. This post may be unsupported."
            )

        image_path = download_direct_image(image_url, temp_dir)
        response = send_file(
            image_path,
            as_attachment=True,
            download_name=get_media_filename(
                image_path, media.get("title", "foger-image")
            ),
        )
        response.call_on_close(
            lambda: shutil.rmtree(temp_dir, ignore_errors=True)
        )
        return response

    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.warning("Image download error: %s", error)
        return jsonify(
            error=str(error) if isinstance(error, ValueError)
            else "Image download failed. The link may be private, unavailable, or unsupported."
        ), 422


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
