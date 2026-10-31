FOGER DOWNLOADER
Frontend + Flask backend

RUN LOCALLY
  pip install -r requirements.txt
  Install FFmpeg and Node.js, then run: python -m flask --app app run
  Open http://127.0.0.1:5000

DEPLOY
  Deploy this folder using the included Dockerfile on a Docker-capable host.
  Main deployment target: Render.

OPTIONAL ENVIRONMENT VARIABLES
  MAX_MB=2048        Maximum downloaded video file size in MB (default 2048).
  IMAGE_MAX_MB=20    Maximum size of each downloaded image in MB (default 20).
  MAX_SECONDS is no longer used: there is no application-level video duration cutoff.

IMPORTANT RESOURCE LIMIT
  Video duration is not limited by the application, but the file-size cap remains
  to protect the server and hosting account. Very long videos can still exceed
  the file-size cap or the host's own request/resource limits. Increase MAX_MB
  only if your hosting plan has enough disk, memory, bandwidth, and request time.

YOUTUBE NOTES
  YouTube Shorts and regular YouTube URLs use the same extractor. YouTube changes
  its delivery and verification requirements frequently, so some videos may still
  be unavailable to yt-dlp. Private, members-only, age-restricted, DRM-protected,
  or otherwise restricted videos are not supported. The app does not bypass those
  restrictions. Use only videos you own or have permission to download.

MAINTENANCE
  Keep yt-dlp updated and rebuild the Docker image after dependency updates.
  Check Render logs for the exact extractor error when a YouTube link fails.
