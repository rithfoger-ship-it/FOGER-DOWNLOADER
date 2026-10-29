FOGER DOWNLOADER (frontend + backend)

RUN LOCALLY
  pip install -r requirements.txt   (also install ffmpeg)
  python -m flask --app app run     -> open http://127.0.0.1:5000

DEPLOY (needs a server that runs Docker or Python - NOT static hosting)
  Render / Railway / Fly.io / any VPS: deploy this folder using the Dockerfile.
  Optional env vars: MAX_SECONDS (default 1800), MAX_MB (default 500).

MAINTENANCE
  Platforms change often. If downloads start failing, update yt-dlp:
  rebuild the image (pip install -U yt-dlp).

NOTES
  - Only public content works. Private/login-only content is not supported.
  - Only allow downloads of content you own or have permission to save.
  - Add Terms/Privacy/DMCA contact pages before opening to the public.
