"""Admin routes — recorder, baseline panel, recordings, publishing, seed."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import require_admin
from app.db import get_db
from app.models import (
    KbAnswer,
    KbChunk,
    KbSource,
    LlmBaseline,
    Question,
    Recording,
    User,
    Verse,
)
from app.models.corpus import TIER_RANK
from app.services import ingestion
from app.services import llm_baselines
from app.services import recordings as rec_service
from app.services.seed import seed_starter
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _primary_verse_for(db: Session, question: Question) -> Verse | None:
    """Resolve the canonical shloka for a question via the gita_reference."""
    if not question.gita_reference:
        return None
    # gita_reference may be a single ref ("2.47") or a range ("2.62-2.63").
    ref = question.gita_reference.split("-")[0].strip()
    return db.execute(select(Verse).where(Verse.verse_ref == ref)).scalar_one_or_none()


def _latest_typed_answer(db: Session, question_id: int) -> KbAnswer | None:
    """Most recent typed (manual-source) answer for a question, if any."""
    return (
        db.execute(
            select(KbAnswer)
            .join(KbSource, KbAnswer.source_id == KbSource.id)
            .where(KbAnswer.question_id == question_id, KbSource.type == "manual")
            .order_by(KbAnswer.id.desc())
        )
        .scalars()
        .first()
    )


def _save_typed_answer(db: Session, question_id: int, tier: str, text: str, user: User) -> KbAnswer:
    """Upsert a typed/pasted/dictated answer for (question, tier).

    The typed text IS the transcript — same field a transcription would fill —
    so it flows through the identical publish → ingest pipeline. Idempotent per
    (question, tier): re-saving updates in place rather than duplicating.
    """
    if tier not in TIER_RANK:
        tier = "seeker"
    answer = (
        db.execute(
            select(KbAnswer)
            .join(KbSource, KbAnswer.source_id == KbSource.id)
            .where(
                KbAnswer.question_id == question_id,
                KbAnswer.tier == tier,
                KbSource.type == "manual",
            )
            .order_by(KbAnswer.id.desc())
        )
        .scalars()
        .first()
    )
    if answer is None:
        source = KbSource(type="manual", title=f"Typed answer · Q{question_id}", uploaded_by=user.id)
        db.add(source)
        db.flush()
        answer = KbAnswer(
            question_id=question_id,
            source_id=source.id,
            tier=tier,
            transcript_raw=text,
            transcript_edited=text,
            answer_final=text,
            status="published",
            version=1,
        )
        db.add(answer)
    else:
        answer.transcript_edited = text
        answer.answer_final = text
        answer.status = "published"
        answer.version = (answer.version or 1) + 1
    db.commit()
    db.refresh(answer)
    return answer


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    def count(model) -> int:
        return db.execute(select(func.count()).select_from(model)).scalar_one()

    stats = {
        "questions": count(Question),
        "verses": count(Verse),
        "recordings": count(Recording),
        "answers": count(KbAnswer),
        "users": count(User),
    }
    return templates.TemplateResponse(
        "admin/dashboard.html", {"request": request, "user": user, "stats": stats}
    )


@router.get("/recorder", response_class=HTMLResponse)
def recorder_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    questions = list(
        db.execute(select(Question).order_by(Question.q_number.nulls_last())).scalars()
    )
    total = 160
    return templates.TemplateResponse(
        "admin/recorder_list.html",
        {
            "request": request,
            "user": user,
            "questions": questions,
            "total": total,
            "tiers": list(TIER_RANK.keys()),
        },
    )


@router.get("/recorder/{question_id}", response_class=HTMLResponse)
def recorder(
    request: Request,
    question_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    question = db.get(Question, question_id)
    if question is None:
        return RedirectResponse("/admin/recorder", status_code=status.HTTP_303_SEE_OTHER)
    verse = _primary_verse_for(db, question)
    baselines = llm_baselines.baselines_for(db, question_id)
    recent = list(
        db.execute(
            select(Recording)
            .where(Recording.question_id == question_id)
            .order_by(Recording.id.desc())
            .limit(5)
        ).scalars()
    )
    # Prefill the answer box with the latest typed answer, else the latest
    # transcript — so a transcribed recording lands in the SAME editable box.
    typed = _latest_typed_answer(db, question_id)
    prefill_text = ""
    prefill_tier = "seeker"
    if typed is not None:
        prefill_text = typed.answer_final or ""
        prefill_tier = typed.tier
    elif recent and recent[0].transcript_text:
        prefill_text = recent[0].transcript_text
    return templates.TemplateResponse(
        "admin/recorder.html",
        {
            "request": request,
            "user": user,
            "question": question,
            "verse": verse,
            "baselines": baselines,
            "providers": llm_baselines.PROVIDERS,
            "recent": recent,
            "tiers": list(TIER_RANK.keys()),
            "prefill_text": prefill_text,
            "prefill_tier": prefill_tier,
        },
    )


@router.post("/recorder/{question_id}/baselines")
def generate_baselines(
    question_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    question = db.get(Question, question_id)
    if question is not None:
        llm_baselines.generate_baselines(db, question)
    return RedirectResponse(f"/admin/recorder/{question_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/recorder/{question_id}/upload")
async def upload_recording(
    question_id: int,
    background: BackgroundTasks,
    audio: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    data = await audio.read()
    suffix = ".webm"
    if audio.filename and "." in audio.filename:
        suffix = "." + audio.filename.rsplit(".", 1)[-1]
    path = rec_service.save_temp(data, suffix=suffix)
    recording = Recording(
        question_id=question_id,
        admin_user_id=user.id,
        railway_temp_path=path,
        status="recording",
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)
    # Transcribe → Drive → delete-temp runs in the background.
    background.add_task(rec_service.process_by_id, recording.id)
    return RedirectResponse(f"/admin/recorder/{question_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/recorder/{question_id}/text")
def save_text_answer(
    question_id: int,
    text: str = Form(...),
    tier: str = Form("seeker"),
    return_to: str = Form("detail"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Type / paste / dictate path: save text directly as a published answer.

    Same destination as a transcribed recording — the text becomes the answer
    and is ready to ingest from the Embeddings page.
    """
    text = (text or "").strip()
    if text:
        _save_typed_answer(db, question_id, tier, text, user)
    dest = "/admin/recorder" if return_to == "list" else f"/admin/recorder/{question_id}"
    return RedirectResponse(dest, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/recordings", response_class=HTMLResponse)
def recordings_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = list(db.execute(select(Recording).order_by(Recording.id.desc()).limit(100)).scalars())
    return templates.TemplateResponse(
        "admin/recordings.html", {"request": request, "user": user, "rows": rows}
    )


@router.post("/recordings/{recording_id}/retry")
def retry_recording(
    recording_id: int,
    background: BackgroundTasks,
    user: User = Depends(require_admin),
):
    background.add_task(rec_service.process_by_id, recording_id)
    return RedirectResponse("/admin/recordings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/recordings/{recording_id}/publish")
def publish_recording(
    recording_id: int,
    tier: str = Form("seeker"),
    answer_final: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Step 7: review/edit/tier-tag/publish → kb_answers (+ provenance source)."""
    recording = db.get(Recording, recording_id)
    if recording is None:
        return RedirectResponse("/admin/recordings", status_code=status.HTTP_303_SEE_OTHER)
    if tier not in TIER_RANK:
        tier = "seeker"
    source = KbSource(
        type="recording",
        title=f"Recording {recording.id}",
        gdrive_file_id=recording.gdrive_file_id,
        audio_url=recording.gdrive_url,
        duration_sec=recording.duration_sec,
        uploaded_by=user.id,
    )
    db.add(source)
    db.flush()
    answer = KbAnswer(
        question_id=recording.question_id,
        source_id=source.id,
        tier=tier,
        transcript_raw=recording.transcript_text,
        transcript_edited=answer_final or recording.transcript_text,
        answer_final=answer_final or recording.transcript_text,
        status="published",
    )
    db.add(answer)
    db.commit()
    return RedirectResponse("/admin/recordings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/seed")
def run_seed(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    seed_starter(db)
    return RedirectResponse("/admin/recorder", status_code=status.HTTP_303_SEE_OTHER)


# --- Embeddings / ingestion (Phase 3) --------------------------------------

@router.get("/embeddings", response_class=HTMLResponse)
def embeddings_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    answers = list(
        db.execute(select(KbAnswer).where(KbAnswer.status == "published").order_by(KbAnswer.id.desc())).scalars()
    )
    rows = [
        {
            "answer": a,
            "question": db.get(Question, a.question_id) if a.question_id else None,
            "chunks": ingestion.chunk_count_for(db, a.id),
        }
        for a in answers
    ]
    total_chunks = db.execute(select(func.count()).select_from(KbChunk)).scalar_one()
    return templates.TemplateResponse(
        "admin/embeddings.html",
        {"request": request, "user": user, "rows": rows, "total_chunks": total_chunks},
    )


@router.post("/embeddings/ingest-all")
def ingest_all(background: BackgroundTasks, user: User = Depends(require_admin)):
    background.add_task(ingestion.ingest_all_in_background)
    return RedirectResponse("/admin/embeddings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/answers/{answer_id}/ingest")
def ingest_one(answer_id: int, background: BackgroundTasks, user: User = Depends(require_admin)):
    background.add_task(ingestion.ingest_answer_by_id, answer_id)
    return RedirectResponse("/admin/embeddings", status_code=status.HTTP_303_SEE_OTHER)
