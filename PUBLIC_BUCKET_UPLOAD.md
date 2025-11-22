## Approve & Copy: Public GCS Upload and Auth

This doc explains how the app authenticates to Google Cloud to save the final email to a public bucket, which endpoint is called from the UI, and how another app can upload assets to the same bucket under a `video_assets/YYYY/MM/DD/` path.

### What the UI calls
- The "Approve & Copy" button in `editor.html` and `review.html` triggers:
  - Method: `POST`
  - Endpoint: `/api/newsletter/{date}/approve` (date format: `YYYY-MM-DD`)

Response payload (JSON):
- `status`: "approved"
- `message`: human-readable status
- `copiable_html`: the final HTML string for clipboard
- `public_url`: public URL of the uploaded final HTML (if a public bucket is configured and accessible)

### What the endpoint does
- Loads the current HTML for the given date.
- Computes hierarchical date path: `newsletters/drafts/YYYY/MM/DD` (e.g., `newsletters/drafts/2025/07/26`).
- Writes the final HTML to the primary bucket path:
  - `newsletters/drafts/YYYY/MM/DD/final_email_beehiiv.html`
- Then attempts to publish the same file to a public bucket if configured; otherwise, it attempts to publicize the object in the primary bucket.

Bucket selection:
- Primary bucket: `BUCKET_NAME`
- Public bucket: `PUBLIC_BUCKET_NAME` if set; else fallback to `BUCKET_NAME`

Public URL behavior:
- The code calls `blob.make_public()` in a best-effort manner. If your bucket uses Uniform Bucket-Level Access or has Public Access Prevention, the call will be skipped/ignored; in that case, public readability must be granted via bucket IAM (e.g., `allUsers: roles/storage.objectViewer`).

### GCP authentication model
The app supports two runtime modes:

1) Production (Cloud Run/Cloud Functions/etc.) with `GOOGLE_CLOUD_PROJECT` set and `FORCE_GCP_MODE` not set
- Uses Application Default Credentials for `google-cloud-storage` via the runtime’s service account.
- Secrets (Flask key, Google OAuth info) are read from Secret Manager.
- Primary bucket name is inferred as: `{GOOGLE_CLOUD_PROJECT}-newsletters`.
- Optional public bucket via `GCP_PUBLIC_BUCKET_NAME`.

2) Forced GCP mode for local/dev (`FORCE_GCP_MODE=true`)
- Loads env from `.env`.
- Uses `google-cloud-storage` `storage.Client()` locally, which relies on ADC (e.g., `gcloud auth application-default login`) or a service account JSON key specified by standard ADC env vars (e.g., `GOOGLE_APPLICATION_CREDENTIALS`).
- Requires `GCP_BUCKET_NAME` to be set for the primary bucket.
- Optional public bucket via `GCP_PUBLIC_BUCKET_NAME`.

Key environment variables
- `GOOGLE_CLOUD_PROJECT`: Project ID for production.
- `FORCE_GCP_MODE`: Set to `true` to force GCP storage in dev.
- `GCP_BUCKET_NAME`: Primary bucket name (required in forced mode).
- `GCP_PUBLIC_BUCKET_NAME`: Optional separate public bucket for artifacts.

Required IAM
- The runtime’s service account (or local credentials) needs write access to the primary (and public) bucket(s): `roles/storage.objectAdmin` or equivalent granularity (`storage.objects.create`, `storage.objects.update`).
- If you intend objects to be publicly readable and are using Uniform Bucket-Level Access, grant bucket-level IAM: `allUsers → roles/storage.objectViewer` on the public bucket.

### Path structure used by this app
- Newsletter final: `newsletters/drafts/YYYY/MM/DD/final_email_beehiiv.html`
- Where `YYYY/MM/DD` comes from transforming `YYYY-MM-DD` by replacing `-` with `/`.

Helper for hierarchical path (conceptual):
```python
def date_to_hierarchical_path(date_str: str) -> str:
    # "2025-07-26" -> "newsletters/drafts/2025/07/26"
    return f"newsletters/drafts/{date_str.replace('-', '/')}"
```

### From another app: upload to the same bucket under video_assets
If a second app needs to save a file to the same bucket with the same date folder structure, use this object path:
- `video_assets/YYYY/MM/DD/<filename>`

Recommended bucket selection logic (same as this app’s public behavior):
- Prefer `GCP_PUBLIC_BUCKET_NAME` if you want assets to be publicly readable.
- Otherwise, use the primary bucket:
  - In production: `{GOOGLE_CLOUD_PROJECT}-newsletters`
  - In forced dev mode: `GCP_BUCKET_NAME`

Minimal Python example
```python
import os
from google.cloud import storage

def upload_video_asset(date_str: str, filename: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """
    Upload to: video_assets/YYYY/MM/DD/{filename}
    Returns a public URL if the bucket is publicly readable or object ACLs are allowed.
    """
    yyyy_mm_dd = date_str.strip()
    hierarchical = yyyy_mm_dd.replace("-", "/")  # e.g., 2025/07/26
    object_name = f"video_assets/{hierarchical}/{filename}"

    # Choose bucket consistent with the app's approach
    bucket_name = (
        os.environ.get("GCP_PUBLIC_BUCKET_NAME")
        or os.environ.get("GCP_BUCKET_NAME")
        or (f"{os.environ['GOOGLE_CLOUD_PROJECT']}-newsletters" if os.environ.get("GOOGLE_CLOUD_PROJECT") else None)
    )
    if not bucket_name:
        raise RuntimeError("No bucket configured. Set GCP_PUBLIC_BUCKET_NAME, GCP_BUCKET_NAME, or GOOGLE_CLOUD_PROJECT.")

    client = storage.Client()  # Uses ADC (service account on Cloud Run, or local ADC)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_string(data, content_type=content_type)

    # Best-effort public exposure (may be a no-op under uniform access)
    try:
        blob.make_public()
    except Exception:
        pass

    # Public URL works if bucket/obj is publicly readable
    return f"https://storage.googleapis.com/{bucket_name}/{object_name}"

# Example usage:
# url = upload_video_asset("2025-07-26", "teaser.mp4", open("teaser.mp4", "rb").read(), content_type="video/mp4")
# print("Public URL:", url)
```

Node/other languages
- Use the equivalent client library with ADC and the same `object_name` pattern `video_assets/YYYY/MM/DD/<filename>`.
- Ensure your runtime service account has write permissions to the target bucket.

### Troubleshooting
- If `public_url` is `None` or not accessible:
  - Confirm `GCP_PUBLIC_BUCKET_NAME` is set to a bucket with public read IAM, or
  - If using the primary bucket, grant public read via IAM (Uniform access) or allow object ACLs and call `make_public`.
- If uploads fail locally:
  - Ensure `gcloud auth application-default login` is configured or `GOOGLE_APPLICATION_CREDENTIALS` points to a valid service account key.
  - In forced mode, confirm `GCP_BUCKET_NAME` is set.


