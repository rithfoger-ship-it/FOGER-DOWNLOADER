
                "removed, or unsupported."
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
                temp_dir, "%(title).100s-%(id)s.%(ext)s"
            ),
            "max_filesize": MAX_BYTES,
            "noplaylist": True,
            "continuedl": True,
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
            options["merge_output_format"] = "mp4/mkv"
        else:
            options["format"] = (
                f"bestvideo*[height<={quality}]"
                f"+bestaudio/best[height<={quality}]/best"
            )
            options["merge_output_format"] = "mp4/mkv"

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


# --------------------------------------------------
# DIRECT IMAGE DOWNLOAD
# --------------------------------------------------

def download_direct_image(url, temp_dir):
    """Download a direct image while validating redirects and file size."""
    current_url = url

    for _ in range(5):
        if not validate_public_image_url(current_url):
            raise ValueError("The image URL is not a public address.")

        try:
            response = requests.get(
                current_url,
                stream=True,
                timeout=(10, 30),
                allow_redirects=False,
                headers={
                    "User-Agent": "Mozilla/5.0 FOGER-DOWNLOADER/1.0"
                },
            )
        except requests.RequestException as error:
            raise ValueError("Could not connect to the image URL.") from error

        if response.is_redirect or response.is_permanent_redirect:
            next_url = response.headers.get("Location")
            response.close()

            if not next_url:
                raise ValueError("Invalid image redirect.")

            current_url = urljoin(current_url, next_url)
            continue

        if response.status_code != 200:
            response.close()
            raise ValueError(
                f"Image server returned HTTP {response.status_code}."
            )

        content_type = (
            response.headers.get("Content-Type", "")
            .split(";")[0]
            .strip()
            .lower()
        )

        extension = IMAGE_TYPES.get(content_type)

        if not extension:
            response.close()
            raise ValueError("The URL did not return a supported image.")

        content_length = response.headers.get("Content-Length")

        if content_length:
            try:
                if int(content_length) > IMAGE_MAX_BYTES:
                    response.close()
                    raise ValueError("Image exceeds the size limit.")
            except ValueError as error:
                response.close()
                if "size limit" in str(error):
                    raise
                raise ValueError("Invalid image size.") from error

        name = safe_filename(
            os.path.basename(urlparse(current_url).path),
            "foger-image",
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
                        raise ValueError("Image exceeds the size limit.")

                    output.write(chunk)
        finally:
            response.close()

        if total == 0:
            raise ValueError("The image file is empty.")

        return file_path

    raise ValueError("Too many image redirects.")
