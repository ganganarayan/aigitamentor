"""Google Drive archive for recordings (Section 6).

Railway holds audio only temporarily; the durable copy lives in Drive. Uses an
installed-app refresh token (no interactive OAuth at runtime). Guarded: raises
RuntimeError if not fully configured.
"""

from __future__ import annotations

import os

from app.config import settings

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def is_configured() -> bool:
    return bool(
        settings.google_drive_client_id
        and settings.google_drive_client_secret
        and settings.google_drive_refresh_token
    )


def _service():
    if not is_configured():
        raise RuntimeError("GOOGLE_DRIVE_* not configured")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=settings.google_drive_refresh_token,
        client_id=settings.google_drive_client_id,
        client_secret=settings.google_drive_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=_SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(path: str, filename: str) -> tuple[str, str | None]:
    """Upload a local file to the configured recordings folder.

    Returns (file_id, web_view_link).
    """
    from googleapiclient.http import MediaFileUpload

    service = _service()
    metadata: dict = {"name": filename}
    if settings.drive_recordings_folder_id:
        metadata["parents"] = [settings.drive_recordings_folder_id]
    media = MediaFileUpload(path, resumable=False)
    created = (
        service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return created["id"], created.get("webViewLink")


def safe_delete(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
