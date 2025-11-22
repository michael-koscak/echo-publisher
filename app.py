import json
import logging
import os
import subprocess
from typing import Dict, Any
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


# Load environment variables from .env if present. Allow .env to override shell.
load_dotenv(override=True)


logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("echo-publisher")

app = Flask(__name__)
app.url_map.strict_slashes = False


SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_env(name: str, required: bool = True) -> str:
    value = os.getenv(name)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def build_client_config() -> Dict[str, Any]:
    client_id = get_env("GOOGLE_CLIENT_ID")
    client_secret = get_env("GOOGLE_CLIENT_SECRET")
    redirect_uri = get_env("OAUTH_REDIRECT_URI")

    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            # Use OAuth v2 endpoint
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


@app.route("/", methods=["GET"])
def index():
    # Simple index and quick visibility into available routes and env presence (no secrets leaked)
    routes = sorted(str(rule) for rule in app.url_map.iter_rules())
    env_status = {
        "GOOGLE_CLIENT_ID": bool(os.getenv("GOOGLE_CLIENT_ID")),
        "GOOGLE_CLIENT_SECRET": bool(os.getenv("GOOGLE_CLIENT_SECRET")),
        "OAUTH_REDIRECT_URI": bool(os.getenv("OAUTH_REDIRECT_URI")),
        "GOOGLE_REFRESH_TOKEN": bool(os.getenv("GOOGLE_REFRESH_TOKEN")),
    }
    return jsonify({"status": "ok", "routes": routes, "env": env_status})


@app.route("/auth/start", methods=["GET"])
def auth_start():
    try:
        client_config = build_client_config()
        redirect_uri = get_env("OAUTH_REDIRECT_URI")

        flow = Flow.from_client_config(client_config=client_config, scopes=SCOPES)
        flow.redirect_uri = redirect_uri

        logger.info("OAuth redirect_uri configured: %s", redirect_uri)
        logger.info("OAuth client_id: %s", client_config.get("web", {}).get("client_id"))

        # include_granted_scopes as lowercase string to satisfy Google's validator
        authorization_url, _state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )

        logger.info("OAuth authorization URL generated: %s", authorization_url)

        logger.info("Starting OAuth flow for YouTube upload scope.")
        logger.info("Redirecting user to Google's consent screen.")
        return redirect(authorization_url)
    except Exception as exc:
        logger.exception("Failed to start OAuth flow: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/auth/callback", methods=["GET"])
def auth_callback():
    error = request.args.get("error")
    if error:
        logger.error("OAuth error from provider: %s", error)
        return (
            "<h1>Auth error</h1><p>Check server logs for details.</p>",
            400,
            {"Content-Type": "text/html"},
        )

    code = request.args.get("code")
    if not code:
        return (
            "<h1>Auth missing code</h1><p>No authorization code provided.</p>",
            400,
            {"Content-Type": "text/html"},
        )

    try:
        client_config = build_client_config()
        redirect_uri = get_env("OAUTH_REDIRECT_URI")

        flow = Flow.from_client_config(client_config=client_config, scopes=SCOPES)
        flow.redirect_uri = redirect_uri

        # Exchange code for tokens
        flow.fetch_token(code=code)
        creds = flow.credentials

        tokens_dict = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
            "expiry": creds.expiry.isoformat() if getattr(creds, "expiry", None) else None,
        }

        logger.info("OAuth token exchange complete. Tokens:")
        print(json.dumps(tokens_dict, indent=2))

        return (
            "<h1>Auth success.</h1><p>Check server logs for tokens.</p>",
            200,
            {"Content-Type": "text/html"},
        )
    except Exception as exc:
        logger.exception("Failed to complete OAuth callback: %s", exc)
        return (
            "<h1>Auth failed.</h1><p>Check server logs for details.</p>",
            500,
            {"Content-Type": "text/html"},
        )


def _make_vertical_thumbnail(src_video: str, out_path: str, ts_seconds: float = 2.75) -> bool:
    """
    Extract a frame at ts_seconds and produce a true 9:16 1080x1920 JPG.
    Returns True on success, False otherwise.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(ts_seconds),
        "-i", src_video,
        "-frames:v", "1",
        "-q:v", "2",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,crop=1080:1920",
        out_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        logger.info("Thumbnail generated: %s (ts=%.2fs)", out_path, ts_seconds)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg failed to generate thumbnail: %s\nSTDERR:\n%s", e, e.stderr.decode("utf-8", "ignore"))
        return False

@app.route("/upload", methods=["POST"])
def upload_video():
    try:
        # Ensure uploads directory exists
        uploads_dir = os.path.join(".", "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        file_path = os.path.join(uploads_dir, "test.mp4")
        # Optional: allow passing a metadata JSON override file via query param (?metadata=uploads/metadata.json)
        metadata_param = request.args.get("metadata")
        metadata_path = (
            metadata_param if metadata_param else os.path.join(uploads_dir, "metadata.json")
        )
        # If default metadata.json is missing, fall back to youtube_metadata.json template
        if not metadata_param and not os.path.isfile(metadata_path):
            alt_path = os.path.join(uploads_dir, "youtube_metadata.json")
            if os.path.isfile(alt_path):
                metadata_path = alt_path
        logger.info(
            "Resolved metadata path: %s (exists=%s)",
            metadata_path,
            os.path.isfile(metadata_path),
        )

        if not os.path.isfile(file_path):
            msg = f"File not found at {file_path}. Place your vertical MP4 there."
            logger.error(msg)
            return jsonify({"error": msg}), 400

        # Generate a 9:16 thumbnail at 2.75s
        thumbnail_path = os.path.join(uploads_dir, "thumbnail_9x16.jpg")
        thumb_ok = _make_vertical_thumbnail(file_path, thumbnail_path, ts_seconds=2.75)

        client_id = get_env("GOOGLE_CLIENT_ID")
        client_secret = get_env("GOOGLE_CLIENT_SECRET")
        refresh_token = get_env("GOOGLE_REFRESH_TOKEN", required=False)

        if not refresh_token:
            msg = (
                "Missing GOOGLE_REFRESH_TOKEN. Run /auth/start, complete consent, "
                "copy refresh_token from logs, and set it in your .env."
            )
            logger.error(msg)
            return jsonify({"error": msg}), 400

        credentials = Credentials(
            None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )

        youtube = build("youtube", "v3", credentials=credentials)

        # Default metadata
        body = {
            "snippet": {
                "title": "Echo & Chamber — How Fox News and MSNBC covered ___ #Shorts",
                "description": (
                    "Full write-up: https://echoandchamber.com/?utm_source=youtube&utm_medium=shorts"
                ),
                "tags": ["news", "media", "politics", "echo and chamber"],
                # For Shorts, YouTube auto-detects vertical when aspect ratio is tall.
            },
            "status": {"privacyStatus": "unlisted"},
        }

        # If a metadata file exists, merge allowed fields over the defaults
        try:
            if metadata_path and os.path.isfile(metadata_path):
                with open(metadata_path, "r", encoding="utf-8") as f:
                    override = json.load(f)
                logger.info("Applying metadata override from: %s", metadata_path)
                if isinstance(override, dict):
                    if isinstance(override.get("snippet"), dict):
                        allowed_snippet_keys = {
                            "title",
                            "description",
                            "tags",
                            "categoryId",
                            "defaultLanguage",
                            "defaultAudioLanguage",
                        }
                        for k, v in override["snippet"].items():
                            if k in allowed_snippet_keys:
                                body["snippet"][k] = v
                    if isinstance(override.get("status"), dict):
                        allowed_status_keys = {
                            "privacyStatus",
                            "selfDeclaredMadeForKids",
                            "license",
                        }
                        for k, v in override["status"].items():
                            if k in allowed_status_keys:
                                body["status"][k] = v
                logger.info(
                    "Final snippet after override: %s",
                    json.dumps(body.get("snippet", {}), ensure_ascii=False),
                )
                logger.info(
                    "Final status after override: %s",
                    json.dumps(body.get("status", {}), ensure_ascii=False),
                )
        except Exception as meta_exc:
            logger.error("Failed to read/parse metadata file '%s': %s", metadata_path, meta_exc)

        media = MediaFileUpload(
            file_path,
            mimetype="video/mp4",
            chunksize=1024 * 1024,
            resumable=True,
        )

        request_insert = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        logger.info("Starting YouTube upload: %s", os.path.basename(file_path))
        response = None
        while response is None:
            status, response = request_insert.next_chunk()
            if status:
                logger.info("Upload progress: %.2f%%", float(status.progress()) * 100)

        video_id = response.get("id")
        watch_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None
        logger.info("Upload completed. Video ID: %s", video_id)

        # --- Set custom thumbnail if available ---
        try:
            if video_id and thumb_ok and os.path.isfile(thumbnail_path):
                logger.info("Setting custom thumbnail: %s", thumbnail_path)
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg", resumable=False),
                ).execute()
                logger.info("Custom thumbnail set.")
            else:
                logger.info("Thumbnail not set (videoId missing or thumbnail generation failed).")
        except HttpError as e:
            logger.exception("Failed to set custom thumbnail: %s", e)

        return jsonify({"videoId": video_id, "watchUrl": watch_url}), 200
    except HttpError as http_err:
        logger.exception("YouTube API error: %s", http_err)
        return jsonify({"error": str(http_err)}), 500
    except Exception as exc:
        logger.exception("Unexpected error during upload: %s", exc)
        return jsonify({"error": str(exc)}), 500


def _print_startup_help():
    logger.info("echo-publisher server running on http://localhost:8080")
    logger.info("Start OAuth: http://localhost:8080/auth/start")
    logger.info("After setting refresh token in .env, upload with:")
    logger.info("curl -X POST http://localhost:8080/upload")


def _log_registered_routes():
    logger.info("Registered routes:")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
        methods = ",".join(sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}))
        logger.info("  %s  methods=[%s]", rule, methods)


if __name__ == "__main__":
    _print_startup_help()
    _log_registered_routes()
    app.run(host="0.0.0.0", port=8080)


