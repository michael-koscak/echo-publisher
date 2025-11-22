# echo-publisher

Minimal Python Flask app to upload a vertical MP4 to YouTube via API.

## How to run

1) Install
```bash
cd /Users/michaelkoscak/Documents/code-projects/echo-publisher
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

2) Configure env (YouTube OAuth, GCS, Instagram)
- Set in `.env` (values provided separately):
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `GOOGLE_REFRESH_TOKEN`
  - `FORCE_GCP_MODE=true` (local) and either `GCP_PUBLIC_BUCKET_NAME` or `GCP_BUCKET_NAME`
  - `instagram_account_ID`, `instagram_access_token`
- For local GCS auth, ensure `gcloud auth application-default login` or `GOOGLE_APPLICATION_CREDENTIALS` is configured.

3) Obtain/refresh the YouTube refresh token (only if needed)
```bash
python app.py           # starts server on http://localhost:8080
# open http://localhost:8080/auth/start, complete consent, copy refresh_token to .env
```

4) Place assets in a date folder and publish
- Folder: `uploads/YYYY/MM/DD/`
- Files: a single `.mp4` and `metadata.json` (see schema below)
```bash
python publish.py --date 2025-11-12
# or explicitly pass a file
python publish.py --date 2025-11-12 --file /absolute/path/to/video.mp4
```

Notes:
- Date `YYYY-MM-DD` maps to `uploads/YYYY/MM/DD/` and GCS path `video_assets/YYYY/MM/DD/<filename>`.
- The CLI uploads to YouTube, uploads the MP4 to GCS (public), then publishes an Instagram Reel and a Feed video.

## Features
- OAuth 2.0 web flow to obtain a refresh token
- Upload endpoint that sends `./uploads/test.mp4` to YouTube as Unlisted
- Simple logs and minimal JSON/HTML responses

## Requirements
- Python 3.10+
- Google Cloud project with YouTube Data API v3 enabled

## Setup
1) Clone and install dependencies:

```bash
cd /Users/michaelkoscak/Documents/code-projects/echo-publisher
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

2) Configure environment:

```bash
cp .env.example .env
# Edit .env with your GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_REDIRECT_URI
```

Notes:
- Create an OAuth 2.0 Client ID of type "Web application" in Google Cloud Console.
- Add your redirect URI (default: `http://localhost:8080/auth/callback`).
- Ensure "YouTube Data API v3" is enabled for your project.

## Run the server
```bash
python app.py
```
Server starts on `http://localhost:8080`.

## Obtain a refresh token
1) Start the OAuth flow:
   - Open `http://localhost:8080/auth/start` in your browser
2) Approve the consent screen and redirect back to the app
3) Check your terminal/server logs — the full tokens dict is printed
4) Copy `refresh_token` into your `.env` as `GOOGLE_REFRESH_TOKEN`

## Upload a video
1) Place your vertical MP4 at `./uploads/test.mp4` (folder is created if missing)
2) (Optional) Add metadata override at `./uploads/metadata.json` to customize title/description/tags/privacy. Alternatively, edit the provided template `./uploads/youtube_metadata.json` and the app will auto-use it if `metadata.json` is missing.
   Example:

```json
{
  "snippet": {
    "title": "Echo & Chamber — How Fox News and MSNBC covered XYZ #Shorts",
    "description": "Full write-up: https://echoandchamber.com/?utm_source=youtube&utm_medium=shorts",
    "tags": ["news", "media", "politics", "echo and chamber"]
  },
  "status": { "privacyStatus": "unlisted" }
}
```

Allowed snippet keys: `title`, `description`, `tags`, `categoryId`, `defaultLanguage`, `defaultAudioLanguage`.
Allowed status keys: `privacyStatus`, `selfDeclaredMadeForKids`, `license`.

3) Call the upload endpoint:

```bash
curl -X POST http://localhost:8080/upload
```

Or specify a custom metadata path (relative to repo root), e.g. `uploads/my_video.json`:

```bash
curl -X POST "http://localhost:8080/upload?metadata=uploads/my_video.json"
```

If paths get confusing, use an absolute path:

```bash
curl -X POST "http://localhost:8080/upload?metadata=/Users/michaelkoscak/Documents/code-projects/echo-publisher/uploads/metadata.json"
```

Metadata tips:
- Precedence: query param `?metadata=...` > `uploads/metadata.json` > `uploads/youtube_metadata.json` (template).
- No server restart is needed for metadata changes.
- The server logs show which metadata file was used and the final `snippet`/`status` payload:
  - “Resolved metadata path: … (exists=true)”
  - “Final snippet after override: …”
  - “Final status after override: …”

Template workflow (recommended if most fields are constant):
- Edit `uploads/youtube_metadata.json` — only change `snippet.title`, `snippet.description`, and `snippet.tags`. The fixed fields remain:
  - `snippet.categoryId=25`, `snippet.defaultLanguage=en`, `snippet.defaultAudioLanguage=en`
  - `status.privacyStatus=public`, `status.selfDeclaredMadeForKids=false`, `status.license=youtube`
- Call `curl -X POST http://localhost:8080/upload`

If successful, the response contains:
```json
{"videoId":"<id>", "watchUrl":"https://www.youtube.com/watch?v=<id>"}
```

## CLI: Publish YouTube + Instagram from a date folder

Run the CLI to publish content stored under `uploads/YYYY/MM/DD/`:

```bash
python publish.py --date 2025-11-12
# or explicitly choose a file
python publish.py --date 2025-11-12 --file /absolute/path/to/video.mp4
```

Folder structure:
- `uploads/YYYY/MM/DD/metadata.json` — metadata for YouTube and Instagram
- `uploads/YYYY/MM/DD/<your_video>.mp4` — single MP4 to publish

Minimal `metadata.json` example:

```json
{
  "youtube": {
    "snippet": {
      "title": "Two completely different stories",
      "description": "Full write-up: https://echoandchamber.com/?utm_source=youtube",
      "tags": ["news","media","echo and chamber"]
    },
    "status": { "privacyStatus": "public", "selfDeclaredMadeForKids": false, "license": "youtube" }
  },
  "instagram": {
    "caption": "Two networks, two stories. Full write-up:",
    "hashtags": ["MediaBias","News"],
    "thumb_offset_seconds": 2.75,
    "share_to_feed": true,
    "enable_reel": true,
    "enable_post": true
  }
}
```

Behavior:
- YouTube upload uses `youtube.snippet` and `youtube.status` with the same allowed keys as the server endpoint.
- Thumbnail: a 9:16 JPG is generated at `thumbnail_9x16.jpg` using `thumb_offset_seconds` (default 2.75s).
- GCS upload: the video is uploaded to `video_assets/YYYY/MM/DD/<filename>` in the preferred bucket (`GCP_PUBLIC_BUCKET_NAME` if set, else `GCP_BUCKET_NAME`), then a public URL is verified.
- Instagram: uses the Instagram Graph API REELS media type. If you request a “post”, we publish a single Reel with `share_to_feed=true` so it also appears on the feed (Instagram deprecated the VIDEO media type). If `instagram.caption` is missing, a caption is composed from YouTube title/description; hashtags fall back to YouTube tags.

Required env:
- YouTube: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
- GCS: `FORCE_GCP_MODE=true` (local) and either `GCP_PUBLIC_BUCKET_NAME` or `GCP_BUCKET_NAME`
- Instagram: `instagram_account_ID` and `instagram_access_token` (or uppercase equivalents)

## Troubleshooting
- Missing refresh token: run the auth flow again and ensure `prompt=consent` is used (it is) and copy the `refresh_token` from logs into `.env`.
- 403/permission errors: ensure YouTube Data API v3 is enabled and the Google account used has access.
- Invalid redirect URI: verify `OAUTH_REDIRECT_URI` in `.env` matches the OAuth client configuration.

## Sample curl
```bash
curl -X POST http://localhost:8080/upload
```

## Re-auth / rotating the refresh token
- If uploads return `401` or you need to switch accounts/consent, do:
  1) Open `http://localhost:8080/auth/start` and complete consent. After redirect, copy the `refresh_token` printed in server logs and update your `.env`.
  2) If you do NOT see a `refresh_token`, remove the app from your Google Account at `https://myaccount.google.com/permissions` (Third‑party access), then repeat step 1. We already request `access_type=offline` and `prompt=consent`.
  3) Restart the server after editing `.env`.

## Quick run commands
```bash
cd /Users/michaelkoscak/Documents/code-projects/echo-publisher
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill CLIENT_ID/SECRET, then later set GOOGLE_REFRESH_TOKEN
python app.py         # visit /auth/start to obtain refresh_token

# After setting refresh token and placing your MP4 (and optional metadata.json):
curl -X POST http://localhost:8080/upload
```

## Security
- Do not commit your `.env` with secrets.


