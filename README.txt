FOGER DOWNLOADER
Frontend + Flask backend. YouTube is NOT supported (removed on purpose).

RUN LOCALLY
  pip install -r requirements.txt
  Install FFmpeg, then run: python -m flask --app app run
  Open http://127.0.0.1:5000

DEPLOY (Render, Docker)
  Deploy this folder using the included Dockerfile.

ENVIRONMENT VARIABLES (set in Render > Environment)
  SITE_URL=https://your-domain.com   Your public address (default: onrender URL)
  CONTACT_EMAIL=you@yourdomain.com   Shown on Contact, Privacy, Terms, DMCA pages
  ADSENSE_PUB_ID=pub-XXXXXXXXXXXXXXXX  Add ONLY after Google gives you the ID.
                                     Enables the AdSense script and /ads.txt.
  MAX_MB=2048                        Max video size in MB (default 2048)
  IMAGE_MAX_MB=20                    Max size of each image in MB (default 20)

PAGES
  /  /about  /privacy  /terms  /dmca  /contact  /ads.txt  /robots.txt  /sitemap.xml

MAINTENANCE
  Keep yt-dlp and gallery-dl updated and rebuild the image regularly.
  Check Render logs for exact extractor errors.
