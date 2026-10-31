
import os
import re
import socket
import shutil
import tempfile
import ipaddress

from urllib.parse import urlparse, urljoin

import requests
import yt_dlp

from flask import Flask, request, jsonify, send_file, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename


# --------------------------------------------------
# APP SETTINGS
# --------------------------------------------------

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://"
)

MAX_BYTES = 2 * 1024 * 1024 * 1024
IMAGE_MAX_BYTES = 20 * 1024 * 1024

IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}

IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"
)

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "tiktok.com",
    "tiktokv.com",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "x.com",
    "twitter.com",
    "vimeo.com",
    "dailymotion.com",
)

BASE_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "restrictfilenames": True,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "file_access_retries": 2,
    "max_filesize": MAX_BYTES,
    "continuedl": True,
    "overwrites": True,
}


# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------

def safe_filename(name, fallback="foger-download"):
    name = secure_filename(str(name or ""))
    return name[:150] or fallback


def get_media_filename(path, title="foger-video"):
    extension = os.path.splitext(path)[1]
    clean_title = safe_filename(title, "foger-video")
    return clean_title + extension


def is_allowed_social_url(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        if parsed.username or parsed.password:
            return False

        host = parsed.hostname.lower().rstrip(".")

        if host == "localhost" or host.endswith(".localhost"):
            return False

        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return False
        except ValueError:
            pass

        return any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )

    except Exception:
        return False


def validate_public_image_url(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        if parsed.username or parsed.password:
            return False

        host = parsed.hostname.lower().rstrip(".")

        if host == "localhost" or host.endswith(".localhost"):
            return False

        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            pass

        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
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


def make_download_response(file_path, temp_dir, title):
    response = send_file(
        file_path,
        as_attachment=True,
        download_name=get_media_filename(file_path, title)
    )

    response.call_on_close(
        lambda: shutil.rmtree(temp_dir, ignore_errors=True)
    )

    return response


# --------------------------------------------------
# HOMEPAGE
# --------------------------------------------------

@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


# --------------------------------------------------
# VIDEO INFORMATION
# --------------------------------------------------

@app.post("/api/info")
@limiter.limit("10 per minute")
def video_info():

    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not is_allowed_social_url(url):
        return jsonify(error="Unsupported video URL."), 400

    try:
        options = dict(BASE_OPTIONS)

        options.update({
            "skip_download": True,
            "extract_flat": False,
        })

        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(
                url,
                download=False
            )

        if not info:
            return jsonify(error="Could not read this media link."), 422

        heights = set()

        for item in info.get("formats", []):
            height = item.get("height")

            if (
                isinstance(height, int)
                and 144 <= height <= 4320
                and item.get("vcodec") != "none"
            ):
                heights.add(height)

        return jsonify({
            "title": info.get("title") or "FOGER Media",
            "thumbnail": info.get("thumbnail"),
            "heights": sorted(heights, reverse=True)
        })

    except Exception as error:
        app.logger.warning("Video info error: %s", error)

        return jsonify(
            error=(
                "Could not read this link. It may be private, "
                "removed, unsupported, or unavailable."
            )
        ), 422


# --------------------------------------------------
# VIDEO DOWNLOAD
# --------------------------------------------------

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
            "outtmpl": os.path.join(
                temp_dir,
                "%(title).100s-%(id)s.%(ext)s"
            ),
            "max_filesize": MAX_BYTES,
            "noplaylist": True,
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
            options["merge_output_format"] = "mp4"

        else:
            options["format"] = (
                f"bestvideo*[height<={quality}]"
                f"+bestaudio/best[height<={quality}]/best"
            )
            options["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(options) as downloader:
            media = downloader.extract_info(
                url,
                download=True
            )

        files = [
            name for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
            and not name.endswith((".part", ".ytdl", ".json"))
        ]

        if not files:
            raise RuntimeError("No media file was created.")

        files.sort(
            key=lambda name: os.path.getsize(
                os.path.join(temp_dir, name)
            ),
            reverse=True
        )

        media_path = os.path.join(temp_dir, files[0])

        return make_download_response(
            media_path,
            temp_dir,
            media.get("title", "foger-video")
        )

    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)

        app.logger.warning("Video download error: %s", error)

        return jsonify(
            error=(
                "Video download failed. The link may be private, "
                "unavailable, unsupported, or too large."
            )
        ), 422


# --------------------------------------------------
# DIRECT PUBLIC IMAGE DOWNLOAD
# --------------------------------------------------

def download_direct_image(url, temp_dir):

    current_url = url

    for _ in range(5):

        if not validate_public_image_url(current_url):
            raise ValueError("Invalid public image URL.")

        try:
            response = requests.get(
                current_url,
                stream=True,
                timeout=(10, 30),
                allow_redirects=False,
                headers={
                    "User-Agent": "Mozilla/5.0 FOGER-DOWNLOADER/1.0"
                }
            )

        except requests.RequestException as error:
            raise ValueError(
                "Could not connect to image URL."
            ) from error

        if response.is_redirect or response.is_permanent_redirect:

            next_url = response.headers.get("Location")
            response.close()

            if not next_url:
                raise ValueError("Invalid image redirect.")

            current_url = urljoin(current_url, next_url)
            continue

        if response.status_code != 200:
            response.close()
            raise ValueError("Image server returned an error.")

        content_type = (
            response.headers.get("Content-Type", "")
            .split(";")[0]
            .strip()
            .lower()
        )

        extension = IMAGE_TYPES.get(content_type)

        if not extension:
            response.close()
            raise ValueError("Unsupported image type.")

        content_length = response.headers.get("Content-Length")

        if content_length:
            try:
                if int(content_length) > IMAGE_MAX_BYTES:
                    response.close()
                    raise ValueError("Image exceeds size limit.")
            except ValueError as error:
                response.close()

                if "size limit" in str(error):
                    raise

                raise ValueError("Invalid image size.") from error

        name = safe_filename(
            os.path.basename(urlparse(current_url).path),
            "foger-image"
        )

        if not name.lower().endswith(IMAGE_EXTENSIONS):
            name += extension

        file_path = os.path.join(temp_dir, name)
        total = 0

        try:
            with open(file_path, "wb") as output:

                for chunk in response.iter_content(64 * 1024):

                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > IMAGE_MAX_BYTES:
                        raise ValueError(
                            "Image exceeds size limit."
                        )

                    output.write(chunk)

        finally:
            response.close()

        if total == 0:
            raise ValueError("Image file is empty.")

        return file_path

    raise ValueError("Too many image redirects.")


# --------------------------------------------------
# IMAGE DOWNLOAD API
# --------------------------------------------------

@app.post("/api/image-download")
@limiter.limit("10 per minute")
def image_download():

    data = request.get_json(silent=True) or {}

    url = str(data.get("url", "")).strip()
    source = str(data.get("source", "direct")).strip().lower()

    if not url:
        return jsonify(error="Please provide an image URL."), 400

    if source == "social":
        return jsonify(
            error=(
                "Social photo extraction is not available yet. "
                "Direct public image URLs are supported."
            )
        ), 422

    if source != "direct":
        return jsonify(error="Unsupported image source."), 400

    temp_dir = tempfile.mkdtemp(prefix="foger_image_")

    try:
        image_path = download_direct_image(url, temp_dir)

        response = send_file(
            image_path,
            as_attachment=True,
            download_name=os.path.basename(image_path)
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
            else "Image download failed. Check the image URL and try again."
        ), 422


# --------------------------------------------------
# OPTIONAL IMAGE API ALIAS
# --------------------------------------------------

@app.get("/api/image")
@limiter.limit("10 per minute")
def image_alias():

    url = request.args.get("url", "").strip()

    if not url:
        return jsonify(error="Please provide an image URL."), 400

    temp_dir = tempfile.mkdtemp(prefix="foger_image_")

    try:
        image_path = download_direct_image(url, temp_dir)

        response = send_file(
            image_path,
            as_attachment=True,
            download_name=os.path.basename(image_path)
        )

        response.call_on_close(
            lambda: shutil.rmtree(temp_dir, ignore_errors=True)
        )

        return response

    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)

        app.logger.warning("Image download error: %s", error)

        return jsonify(
            error="Image download failed. Check the image URL and try again."
        ), 422


# --------------------------------------------------
# ERROR HANDLERS
# --------------------------------------------------

@app.errorhandler(404)
def not_found(error):
    return jsonify(error="Endpoint not found."), 404


@app.errorhandler(429)
def too_many_requests(error):
    return jsonify(
        error="Too many requests. Please try again later."
    ), 429


@app.errorhandler(500)
def server_error(error):
    app.logger.exception("Internal server error")
    return jsonify(error="Internal server error."), 500


# --------------------------------------------------
# LOCAL DEVELOPMENT
# --------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        debug=False
    )
