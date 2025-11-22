import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.cloud import storage

# Load environment variables from .env if present (local/dev convenience)
load_dotenv(override=True)

logger = logging.getLogger("publisher")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
IG_API_BASE = "https://graph.instagram.com/v23.0"


def get_env(name: str, required: bool = True) -> str:
    value = os.getenv(name) or os.getenv(name.lower()) or os.getenv(name.upper())
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def resolve_date_folder(date_str: str) -> Path:
    """
    "2025-11-12" -> <repo>/uploads/2025/11/12
    """
    yyyy, mm, dd = date_str.split("-")
    root = Path(__file__).resolve().parent
    folder = root / "uploads" / yyyy / mm / dd
    if not folder.exists():
        raise FileNotFoundError(f"Date folder not found: {folder}")
    return folder


def find_video_file(date_folder: Path, override: Optional[str] = None) -> Path:
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Video file not found: {override}")
        return p
    mp4s = sorted(date_folder.glob("*.mp4"))
    if not mp4s:
        raise FileNotFoundError(f"No .mp4 found in {date_folder}")
    if len(mp4s) > 1:
        # Pick the most recently modified to avoid ambiguity
        mp4s.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        logger.warning(
            "Multiple .mp4 files found in %s. Picking most recently modified: %s",
            date_folder,
            mp4s[0].name,
        )
    return mp4s[0]


def read_metadata(date_folder: Path) -> Dict[str, Any]:
    path = date_folder / "metadata.json"
    if not path.is_file():
        logger.info("metadata.json not found at %s; proceeding with defaults.", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception as exc:
            raise RuntimeError(f"Failed to parse {path}: {exc}") from exc


def _make_vertical_thumbnail(src_video: str, out_path: str, ts_seconds: float = 2.75) -> bool:
    """
    Extract a frame and produce a true 9:16 1080x1920 JPG.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(ts_seconds),
        "-i",
        src_video,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,crop=1080:1920",
        out_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        logger.info("Thumbnail generated at %s (ts=%.2fs)", out_path, ts_seconds)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg failed to generate thumbnail: %s", e)
        return False


def _allowed_overrides(src: Dict[str, Any], dest: Dict[str, Any], allowed: List[str]) -> None:
    for k, v in src.items():
        if k in allowed:
            dest[k] = v


def prepare_youtube_body(meta: Dict[str, Any]) -> Dict[str, Any]:
    defaults = {
        "snippet": {
            "title": "Echo & Chamber — How Fox News and MSNBC covered ___ #Shorts",
            "description": "Full write-up: https://echoandchamber.com/?utm_source=youtube&utm_medium=shorts",
            "tags": ["news", "media", "politics", "echo and chamber"],
        },
        "status": {"privacyStatus": "unlisted"},
    }
    youtube_meta = meta.get("youtube")
    if isinstance(youtube_meta, dict):
        if isinstance(youtube_meta.get("snippet"), dict):
            _allowed_overrides(
                youtube_meta["snippet"],
                defaults["snippet"],
                [
                    "title",
                    "description",
                    "tags",
                    "categoryId",
                    "defaultLanguage",
                    "defaultAudioLanguage",
                ],
            )
        if isinstance(youtube_meta.get("status"), dict):
            _allowed_overrides(
                youtube_meta["status"],
                defaults["status"],
                ["privacyStatus", "selfDeclaredMadeForKids", "license"],
            )
    return defaults


def youtube_upload(video_path: Path, meta: Dict[str, Any], thumbnail_path: Optional[Path]) -> Dict[str, Any]:
    client_id = get_env("GOOGLE_CLIENT_ID")
    client_secret = get_env("GOOGLE_CLIENT_SECRET")
    refresh_token = get_env("GOOGLE_REFRESH_TOKEN", required=True)

    credentials = Credentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=YOUTUBE_SCOPES,
    )
    youtube = build("youtube", "v3", credentials=credentials)

    body = prepare_youtube_body(meta)

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=1024 * 1024,
        resumable=True,
    )
    request_insert = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    logger.info("Uploading to YouTube: %s", video_path.name)
    response = None
    while response is None:
        status, response = request_insert.next_chunk()
        if status:
            logger.info("YouTube upload progress: %.2f%%", float(status.progress()) * 100)

    video_id = response.get("id")
    watch_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None

    # Set thumbnail if generated
    try:
        if video_id and thumbnail_path and thumbnail_path.is_file():
            logger.info("Setting YouTube custom thumbnail: %s", thumbnail_path)
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg", resumable=False),
            ).execute()
    except HttpError as e:
        logger.warning("Failed to set custom thumbnail: %s", e)

    return {"videoId": video_id, "watchUrl": watch_url}


def pick_bucket_name() -> str:
    bucket = os.getenv("GCP_PUBLIC_BUCKET_NAME") or os.getenv("GCP_BUCKET_NAME")
    if not bucket:
        raise RuntimeError(
            "No bucket configured. Set GCP_PUBLIC_BUCKET_NAME or GCP_BUCKET_NAME (FORCE_GCP_MODE suggested for local)."
        )
    return bucket


def gcs_upload_public(date_str: str, local_path: Path) -> str:
    """
    Uploads to video_assets/YYYY/MM/DD/<filename> and returns the public URL.
    """
    hierarchical = date_str.replace("-", "/")
    object_name = f"video_assets/{hierarchical}/{local_path.name}"
    bucket_name = pick_bucket_name()

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    logger.info("Uploading to GCS: gs://%s/%s", bucket_name, object_name)
    blob.upload_from_filename(str(local_path), content_type="video/mp4")

    # Best-effort public
    try:
        blob.make_public()
    except Exception:
        pass

    public_url = f"https://storage.googleapis.com/{bucket_name}/{object_name}"

    # Verify readability (fail fast if not publicly accessible)
    try:
        r = requests.head(public_url, timeout=10)
        if r.status_code >= 400:
            raise RuntimeError(
                f"GCS object not publicly readable (status {r.status_code}). Ensure bucket/object is public: {public_url}"
            )
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to verify public URL accessibility: {exc}") from exc

    logger.info("Public GCS URL: %s", public_url)
    return public_url


def _compose_caption(meta: Dict[str, Any]) -> str:
    ig = meta.get("instagram") or {}
    caption = (ig.get("caption") or "").strip()

    hashtags: List[str] = []
    if isinstance(ig.get("hashtags"), list):
        hashtags = [str(h).lstrip("#").replace(" ", "") for h in ig["hashtags"] if str(h).strip()]

    if not caption:
        youtube_meta = prepare_youtube_body(meta)
        snippet = youtube_meta.get("snippet", {})
        title = snippet.get("title") or ""
        desc = snippet.get("description") or ""
        caption = title
        if desc:
            caption = f"{title}\n\n{desc}"
        if not hashtags and isinstance(snippet.get("tags"), list):
            hashtags = [str(t).replace(" ", "") for t in snippet["tags"]]

    if hashtags:
        caption = f"{caption}\n\n" + " ".join(f"#{h}" for h in hashtags)
    return caption.strip()


def ig_create_container(
    account_id: str, access_token: str, payload: Dict[str, Any]
) -> str:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    url = f"{IG_API_BASE}/{account_id}/media"
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"IG create container failed: {resp.status_code} {resp.text}")
    data = resp.json()
    return str(data.get("id"))


def ig_poll_status(creation_id: str, access_token: str, timeout_seconds: int = 60) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{IG_API_BASE}/{creation_id}"
    params = {"fields": "status_code"}
    started = time.time()
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code >= 400:
            raise RuntimeError(f"IG poll failed: {resp.status_code} {resp.text}")
        status = (resp.json() or {}).get("status_code") or ""
        if status in {"FINISHED", "ERROR"}:
            return status
        if time.time() - started > timeout_seconds:
            raise TimeoutError("Timed out waiting for IG processing.")
        time.sleep(3)


def ig_publish_container(account_id: str, access_token: str, creation_id: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    url = f"{IG_API_BASE}/{account_id}/media_publish"
    resp = requests.post(url, headers=headers, json={"creation_id": creation_id}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"IG publish failed: {resp.status_code} {resp.text}")
    data = resp.json()
    return str(data.get("id"))


def publish_instagram_variants(
    date_str: str, public_video_url: str, meta: Dict[str, Any]
) -> Dict[str, Any]:
    account_id = get_env("instagram_account_ID") or get_env("INSTAGRAM_ACCOUNT_ID")
    access_token = get_env("instagram_access_token") or get_env("INSTAGRAM_ACCESS_TOKEN")

    ig = meta.get("instagram") or {}
    caption = _compose_caption(meta)
    share_to_feed_meta = bool(ig.get("share_to_feed", True))
    enable_reel = bool(ig.get("enable_reel", True))
    enable_post = bool(ig.get("enable_post", True))

    results: Dict[str, Any] = {"reel": None, "post": None}

    # Instagram deprecates media_type=VIDEO. Use REELS for both Reel and Feed.
    # If a "post" is requested, force share_to_feed=True so it appears on the feed.
    if not (enable_reel or enable_post):
        logger.info("Instagram publishing disabled by metadata.")
        return results

    share_to_feed_final = True if enable_post else share_to_feed_meta

    logger.info(
        "Creating Instagram REELS container (share_to_feed=%s) to satisfy %s",
        share_to_feed_final,
        "reel+post" if (enable_reel and enable_post) else ("post" if enable_post else "reel"),
    )
    container_id = ig_create_container(
        account_id,
        access_token,
        {"video_url": public_video_url, "media_type": "REELS", "caption": caption, "share_to_feed": share_to_feed_final},
    )
    status = ig_poll_status(container_id, access_token)
    if status != "FINISHED":
        raise RuntimeError(f"Instagram processing failed with status: {status}")
    publish_id = ig_publish_container(account_id, access_token, container_id)

    info = {"creation_id": container_id, "publish_id": publish_id}
    if enable_reel:
        results["reel"] = info
    if enable_post:
        results["post"] = info
    logger.info("Instagram published (publish_id=%s)", publish_id)

    return results


@dataclass
class RunResult:
    date: str
    video_file: str
    youtube: Dict[str, Any]
    gcs_public_url: str
    instagram: Dict[str, Any]


def run(date_str: str, file_override: Optional[str] = None) -> RunResult:
    date_folder = resolve_date_folder(date_str)
    video_path = find_video_file(date_folder, file_override)
    meta = read_metadata(date_folder)

    # Thumbnail generation (kept for now, though YouTube upload is temporarily skipped)
    ig = meta.get("instagram") or {}
    thumb_ts = float(ig.get("thumb_offset_seconds", 2.75))
    thumbnail_path = date_folder / "thumbnail_9x16.jpg"
    _make_vertical_thumbnail(str(video_path), str(thumbnail_path), ts_seconds=thumb_ts)

    # Temporarily skip YouTube upload. Leave the call commented for easy re-enable later.
    # yt_result = youtube_upload(video_path, meta, thumbnail_path)
    yt_result = {"skipped": True}
    gcs_url = gcs_upload_public(date_str, video_path)
    ig_result = publish_instagram_variants(date_str, gcs_url, meta)

    return RunResult(
        date=date_str,
        video_file=str(video_path),
        youtube=yt_result,
        gcs_public_url=gcs_url,
        instagram=ig_result,
    )


