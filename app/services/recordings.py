"""Recorder lifecycle orchestration (Section 6, exact steps 3–6).

    record → upload to Railway temp → transcribe from temp → copy to Drive →
    delete the Railway temp file → review/edit/tier-tag/publish.

Each step advances ``recordings.status`` and records ``error_text`` on failure.
Transcription/Drive being unconfigured is a soft failure, not a crash: the
recording row simply parks at a status the admin can retry from.
"""

from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Recording
from app.services import drive, transcription

logger = logging.getLogger("app.recordings")


def _tmp_dir() -> str:
    os.makedirs(settings.recordings_tmp_dir, exist_ok=True)
    return settings.recordings_tmp_dir


def save_temp(data: bytes, suffix: str = ".webm") -> str:
    path = os.path.join(_tmp_dir(), f"rec_{uuid.uuid4().hex}{suffix}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def process_recording(db: Session, recording: Recording) -> Recording:
    """Run transcribe → Drive copy → delete temp for a freshly uploaded file."""
    path = recording.railway_temp_path
    if not path or not os.path.exists(path):
        recording.status = "failed"
        recording.error_text = "Temp file missing."
        db.commit()
        return recording

    # 4) Transcribe from temp.
    try:
        recording.status = "transcribing"
        db.commit()
        recording.transcript_text = transcription.transcribe_file(path)
        recording.status = "transcribed"
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed for recording %s: %s", recording.id, exc)
        recording.status = "failed"
        recording.error_text = f"transcription: {exc}"
        db.commit()
        # Keep the temp file so the admin can retry; do not delete on failure.
        return recording

    # 5) Copy to Google Drive (if configured).
    if drive.is_configured():
        try:
            filename = f"recording_{recording.id}_{os.path.basename(path)}"
            file_id, url = drive.upload_file(path, filename)
            recording.gdrive_file_id = file_id
            recording.gdrive_url = url
            recording.status = "uploaded_drive"
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Drive upload failed for recording %s: %s", recording.id, exc)
            recording.error_text = f"drive: {exc}"
            db.commit()
            return recording  # keep temp for retry
    else:
        recording.error_text = "Drive not configured — temp retained."
        db.commit()
        return recording

    # 6) Delete the Railway temp file once Drive copy + transcript are confirmed.
    drive.safe_delete(path)
    recording.railway_temp_path = None
    recording.status = "temp_deleted"
    db.commit()
    return recording


def process_by_id(recording_id: int) -> None:
    """Background-task entry point — opens its own session (the request's is gone)."""
    from app.db import SessionLocal

    if SessionLocal is None:
        return
    with SessionLocal() as db:
        rec = db.get(Recording, recording_id)
        if rec is not None:
            process_recording(db, rec)
