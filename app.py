
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import urlparse, unquote, urljoin

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

IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
)

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


def is_tiktok_photo_url(url):
    """Identify TikTok photo-post links."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in ("http", "https")
            and (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
            )
            and "/photo/" in parsed.path.lower()
        )
    except Exception:
        return False


def validate_public_image_url(url):
    """Reject local and non-public hosts before requesting an image."""
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
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
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


def looks_like_image_url(url):
    """Check common image extensions without trusting the URL alone."""
    try:
        path = urlparse(str(url)).path.lower()
        return path.endswith(IMAGE_EXTENSIONS)
    except Exception:
        return False


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
        return jsonify(
            error="Please enter a supported public video link."
        ), 400

    # A photo carousel is not a normal video-info request.
    if is_tiktok_photo_url(url):
        return jsonify(
            error=(
                "This is a TikTok photo post. Use the image-download "
                "feature to download its photos."
            )
        ), 422

    try:
        with yt_dlp.YoutubeDL(BASE_OPTIONS) as downloader:
            media = downloader.extract_info(url, download=False)

        if not media:
            return jsonify(error="Could not read this link."), 422

        duration = media.get("duration") or 0

        if duration > MAX_SECONDS:
            return jsonify(
                error="This video is longer than the allowed duration."
            ), 413

        heights = sorted({
            int(item["height"])
            for item in (media.get("formats") or [])
            if item.get("height") and item.get("vcodec") != "none"
        }, reverse=True)

        preferred = [
            height for height in (2160, 1440, 1080, 720, 480, 360)
            if height in heights
        ]
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
            error=(
                "Could not read this link. It may be private, "
                "removed, or unsupported."
            )
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
            "outtmpl": os.path.join(
                temp_dir, "%(title).100s-%(id)s.%(ext)s"
            ),
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
            key=lambda name: os.path.getsize(
                os.path.join(temp_dir, name)
            ),
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
            error=(
                "Video download failed. The link may be private, "
                "unavailable, unsupported, or too large."
            )
        ), 422


def download_direct_image(url, temp_dir):
    """Download a direct image while validating redirects and file size."""
    current_url = url

    for _ in range(5):
        if not validate_public_image_url(current_url):
            raise ValueError("This image URL is not allowed.")

        with requests.get(
            current_url,
            stream=True,
            timeout=(10, 30),
            allow_redirects=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; FOGER-DOWNLOADER/1.0)"
                )
            },
        ) as response:

            if response.is_redirect:
                location = response.headers.get("Location")

                if not location:
                    raise ValueError("Invalid image redirect.")

                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            ).split(";")[0].lower()

            if content_type not in IMAGE_TYPES:
                raise ValueError(
                    "The link did not return a supported image."
                )

            length = response.headers.get("Content-Length")

            if length:
                try:
                    content_length = int(length)
                except (ValueError, TypeError):
                    content_length = None

                if (
                    content_length is not None
                    and content_length > IMAGE_MAX_BYTES
                ):
                    raise ValueError("Image exceeds the size limit.")

            extension = IMAGE_TYPES[content_type]
            path = urlparse(current_url).path
            name = safe_filename(
                os.path.basename(unquote(path)), "image"
            )

            if not name.lower().endswith(extension):
                name = os.path.splitext(name)[0] + extension

            file_path = os.path.join(temp_dir, name)
            total = 0

            with open(file_path, "wb") as output:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > IMAGE_MAX_BYTES:
                        raise ValueError(
                            "Image exceeds the size limit."
                        )

                    output.write(chunk)

            if total == 0:
                raise ValueError("The image file is empty.")

            return file_path

    raise ValueError("Too many image redirects.")


def add_candidate(candidates, url):
    """Keep unique HTTP(S) image candidates."""
    if not isinstance(url, str):
        return

    url = url.strip()

    if not url.startswith(("https://", "http://")):
        return

    if url not in candidates:
        candidates.append(url)


def collect_image_candidates(media, candidates=None, depth=0):
    """Collect thumbnails and direct image URLs from extractor metadata."""
    if candidates is None:
        candidates = []

    if not isinstance(media, dict) or depth > 8:
        return candidates

    entries = media.get("entries")

    if entries:
        for entry in entries:
            if isinstance(entry, dict):
                collect_image_candidates(
                    entry, candidates, depth + 1
                )

        if candidates:
            return candidates

    media_url = media.get("url")

    if isinstance(media_url, str) and looks_like_image_url(media_url):
        add_candidate(candidates, media_url)

    for item in media.get("formats") or []:
        if not isinstance(item, dict):
            continue

        format_url = item.get("url")

        if isinstance(format_url, str) and looks_like_image_url(format_url):
            add_candidate(candidates, format_url)

    thumbnail = media.get("thumbnail")

    if thumbnail:
        add_candidate(candidates, thumbnail)
    else:
        thumbnails = media.get("thumbnails") or []

        if thumbnails:
            sorted_thumbnails = sorted(
                [item for item in thumbnails if isinstance(item, dict)],
                key=lambda item: (
                    (item.get("width") or 0)
                    * (item.get("height") or 0)
                ),
                reverse=True,
            )

            if sorted_thumbnails:
                add_candidate(
                    candidates, sorted_thumbnails[0].get("url")
                )

    return candidates


def download_tiktok_photos(url, temp_dir):
    """Use gallery-dl to download photos from a TikTok photo post."""
    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "-D",
        temp_dir,
        url,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            "TikTok photo download timed out. Please try again."
        ) from error
    except Exception as error:
        app.logger.exception("Could not start gallery-dl")
        raise ValueError(
            "Could not start the TikTok photo downloader."
        ) from error

    downloaded = []

    for root, _, filenames in os.walk(temp_dir):
        for filename in filenames:
            if filename.lower().endswith(IMAGE_EXTENSIONS):
                downloaded.append(os.path.join(root, filename))

    downloaded.sort()

    if result.returncode != 0 or not downloaded:
        detail = (result.stderr or result.stdout or "").strip()

        app.logger.warning(
            "gallery-dl failed (exit %s): %s",
            result.returncode,
            detail[-2000:],
        )

        if not downloaded:
            raise ValueError(
                "TikTok photos could not be extracted. Check the "
                "Render logs for the gallery-dl error. The post may "
                "be private, unavailable, or blocked by TikTok."
            )

    return downloaded


def send_temp_file(path, temp_dir, download_name, mimetype=None):
    """Send a file and remove its temporary directory after response."""
    response = send_file(
        path,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype,
    )

    response.call_on_close(
        lambda: shutil.rmtree(temp_dir, ignore_errors=True)
    )
    return response


@app.post("/api/image-download")
@limiter.limit("10 per minute")
def download_image():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    mode = str(data.get("source", "direct")).strip().lower()

    if mode == "direct":
        if not url.startswith(("http://", "https://")):
            return jsonify(error="Please enter a valid image URL."), 400

        temp_dir = tempfile.mkdtemp(prefix="foger_image_")

        try:
            image_path = download_direct_image(url, temp_dir)
            return send_temp_file(
                image_path,
                temp_dir,
                os.path.basename(image_path),
            )
        except Exception as error:
            shutil.rmtree(temp_dir, ignore_errors=True)
            app.logger.warning("Direct image error: %s", error)
            return jsonify(error=str(error)), 422

    if mode != "social" or not is_allowed_social_url(url):
        return jsonify(
            error="Please enter a supported public image or post URL."
        ), 400

    temp_dir = tempfile.mkdtemp(prefix="foger_social_images_")

    try:
        # TikTok photo carousel: use gallery-dl, not yt-dlp.
        if is_tiktok_photo_url(url):
            photos = download_tiktok_photos(url, temp_dir)

            zip_path = os.path.join(
                temp_dir, "foger-tiktok-photos.zip"
            )

            with zipfile.ZipFile(
                zip_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for index, photo_path in enumerate(photos, start=1):
                    extension = (
                        os.path.splitext(photo_path)[1].lower()
                    )

                    archive.write(
                        photo_path,
                        arcname=f"foger-photo-{index:02d}{extension}",
                    )

            return send_temp_file(
                zip_path,
                temp_dir,
                "foger-tiktok-photos.zip",
                mimetype="application/zip",
            )

        # Keep the existing yt-dlp image-extraction path for other links.
        options = dict(BASE_OPTIONS)
        options["noplaylist"] = False

        with yt_dlp.YoutubeDL(options) as downloader:
            media = downloader.extract_info(url, download=False)

        if not media:
            raise ValueError("No media information was found.")

        candidates = collect_image_candidates(media)

        if not candidates:
            raise ValueError(
                "No public image was found. This post may not expose "
                "its photos to yt-dlp."
            )

        downloaded = []
        failures = []

        for index, image_url in enumerate(candidates[:50], start=1):
            try:
                image_path = download_direct_image(image_url, temp_dir)

                extension = os.path.splitext(image_path)[1] or ".jpg"
                numbered_path = os.path.join(
                    temp_dir, f"foger-photo-{index:02d}{extension}"
                )

                if os.path.abspath(image_path) != os.path.abspath(numbered_path):
                    if os.path.exists(numbered_path):
                        os.remove(numbered_path)
                    os.replace(image_path, numbered_path)

                downloaded.append(numbered_path)

            except Exception as error:
                failures.append(str(error))
                app.logger.warning(
                    "Could not fetch image candidate %s: %s",
                    index,
                    error,
                )

        if not downloaded:
            detail = (
                failures[0]
                if failures
                else "No image could be fetched."
            )
            raise ValueError(
                "The post did not provide a downloadable public image. "
                + detail
            )

        if len(downloaded) == 1:
            image_path = downloaded[0]
            return send_temp_file(
                image_path,
                temp_dir,
                os.path.basename(image_path),
            )

        zip_path = os.path.join(
            temp_dir, "foger-social-photos.zip"
        )

        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for image_path in downloaded:
                archive.write(
                    image_path,
                    arcname=os.path.basename(image_path),
                )

        return send_temp_file(
            zip_path,
            temp_dir,
            "foger-social-photos.zip",
            mimetype="application/zip",
        )

    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.warning("Social image download error: %s", error)

        return jsonify(error=str(error)), 422


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
