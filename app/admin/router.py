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
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth.deps import require_admin
from app.db import get_db
from app.models import (
    AiConfig,
    Contact,
    Conversation,
    KbAnswer,
    KbChunk,
    KbSource,
    LlmBaseline,
    Message,
    Payment,
    Question,
    Recording,
    Subscription,
    User,
    Verse,
)
from app.models.corpus import TIER_RANK
from app.services import ai_settings
from app.services import chat as chat_service
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


@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = list(db.execute(select(User).order_by(User.id.desc()).limit(300)).scalars())
    return templates.TemplateResponse(
        "admin/users.html", {"request": request, "user": user, "rows": rows}
    )


@router.get("/recorder", response_class=HTMLResponse)
def recorder_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    questions = list(
        db.execute(select(Question).order_by(Question.q_number.nulls_last())).scalars()
    )
    # Which tiers already have a published answer, per question (drives the ticks).
    done: dict[int, list[str]] = {}
    pairs = db.execute(
        select(KbAnswer.question_id, KbAnswer.tier)
        .where(KbAnswer.status == "published", KbAnswer.question_id.is_not(None))
        .distinct()
    ).all()
    for qid, tier in pairs:
        done.setdefault(qid, []).append(tier)
    total = 160
    return templates.TemplateResponse(
        "admin/recorder_list.html",
        {
            "request": request,
            "user": user,
            "questions": questions,
            "total": total,
            "tiers": list(TIER_RANK.keys()),
            "done_tiers": done,
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


# --- Answers — view & edit saved/transcribed answers -----------------------

_STATUSES = ["draft", "reviewed", "published"]


@router.get("/answers", response_class=HTMLResponse)
def answers_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    answers = list(db.execute(select(KbAnswer).order_by(KbAnswer.id.desc())).scalars())
    rows = []
    for a in answers:
        rows.append(
            {
                "answer": a,
                "question": db.get(Question, a.question_id) if a.question_id else None,
                "source": db.get(KbSource, a.source_id) if a.source_id else None,
                "chunks": ingestion.chunk_count_for(db, a.id),
            }
        )
    return templates.TemplateResponse(
        "admin/answers.html", {"request": request, "user": user, "rows": rows}
    )


@router.get("/answers/{answer_id}/edit", response_class=HTMLResponse)
def answer_edit_page(
    request: Request,
    answer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    answer = db.get(KbAnswer, answer_id)
    if answer is None:
        return RedirectResponse("/admin/answers", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "admin/answer_edit.html",
        {
            "request": request,
            "user": user,
            "answer": answer,
            "question": db.get(Question, answer.question_id) if answer.question_id else None,
            "source": db.get(KbSource, answer.source_id) if answer.source_id else None,
            "tiers": list(TIER_RANK.keys()),
            "statuses": _STATUSES,
            "chunks": ingestion.chunk_count_for(db, answer.id),
        },
    )


@router.post("/answers/{answer_id}/edit")
def answer_edit_save(
    answer_id: int,
    background: BackgroundTasks,
    text: str = Form(...),
    tier: str = Form("seeker"),
    answer_status: str = Form("published"),
    reingest: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    answer = db.get(KbAnswer, answer_id)
    if answer is None:
        return RedirectResponse("/admin/answers", status_code=status.HTTP_303_SEE_OTHER)
    if tier not in TIER_RANK:
        tier = "seeker"
    if answer_status not in _STATUSES:
        answer_status = "published"
    answer.answer_final = (text or "").strip()
    answer.transcript_edited = answer.answer_final
    answer.tier = tier
    answer.status = answer_status
    answer.version = (answer.version or 1) + 1
    db.commit()
    # Keep the gated corpus consistent: only published answers stay vectorized.
    if answer_status != "published":
        db.execute(delete(KbChunk).where(KbChunk.answer_id == answer.id))
        db.commit()
    elif reingest:
        background.add_task(ingestion.ingest_answer_by_id, answer.id)
    return RedirectResponse("/admin/answers", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/answers/{answer_id}/delete")
def answer_delete(
    answer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    answer = db.get(KbAnswer, answer_id)
    if answer is not None:
        db.delete(answer)  # kb_chunks cascade via FK
        db.commit()
    return RedirectResponse("/admin/answers", status_code=status.HTTP_303_SEE_OTHER)


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


# --- Settings → AI (runtime, DB-backed) ------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    error: str | None = None,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/settings.html", {"request": request, "user": user, "ai": ai_settings.view(db), "error": error}
    )


@router.post("/settings")
def settings_save(
    model_admin: str = Form(""),
    model_free: str = Form(""),
    model_paid: str = Form(""),
    embedding_model: str = Form(""),
    transcribe_model: str = Form(""),
    baseline_providers: list[str] = Form(default=[]),
    anthropic_api_key: str = Form(""),
    openai_api_key: str = Form(""),
    gemini_api_key: str = Form(""),
    perplexity_api_key: str = Form(""),
    clear_anthropic: str = Form(""),
    clear_openai: str = Form(""),
    clear_gemini: str = Form(""),
    clear_perplexity: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Apply any new keys first so validation can use them.
    key_updates = {
        "anthropic": anthropic_api_key.strip(),
        "openai": openai_api_key.strip(),
        "gemini": gemini_api_key.strip(),
        "perplexity": perplexity_api_key.strip(),
    }
    key_clears = [
        provider
        for provider, flag in [
            ("anthropic", clear_anthropic),
            ("openai", clear_openai),
            ("gemini", clear_gemini),
            ("perplexity", clear_perplexity),
        ]
        if flag
    ]
    models = [model_admin.strip(), model_free.strip(), model_paid.strip()]
    ai_settings.update(
        db,
        model_admin=model_admin.strip(),
        model_free=model_free.strip(),
        model_paid=model_paid.strip(),
        embedding_model=embedding_model.strip(),
        transcribe_model=transcribe_model.strip(),
        baseline_providers=baseline_providers,
        key_updates=key_updates,
        key_clears=key_clears,
    )
    # Validate chat model ids against the Models API (typos can't silently break chat).
    error = ai_settings.validate_chat_models(db, models)
    if error:
        return RedirectResponse(f"/admin/settings?error={error}", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/admin/settings", status_code=status.HTTP_303_SEE_OTHER)


# --- AI Config (versioned system prompt) -----------------------------------

@router.get("/ai-config", response_class=HTMLResponse)
def ai_config_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    active = db.execute(
        select(AiConfig).where(AiConfig.active.is_(True)).order_by(AiConfig.version.desc())
    ).scalars().first()
    versions = db.execute(select(AiConfig).order_by(AiConfig.version.desc()).limit(20)).scalars().all()
    return templates.TemplateResponse(
        "admin/ai_config.html",
        {
            "request": request,
            "user": user,
            "prompt": active.system_prompt if active else chat_service.DEFAULT_SYSTEM_PROMPT,
            "active": active,
            "using_default": active is None,
            "versions": versions,
        },
    )


@router.post("/ai-config")
def ai_config_save(
    system_prompt: str = Form(...),
    temperature: str = Form("0.7"),
    top_k: str = Form("8"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        temp = float(temperature)
    except ValueError:
        temp = 0.7
    try:
        topk = int(top_k)
    except ValueError:
        topk = 8
    next_version = (db.execute(select(func.coalesce(func.max(AiConfig.version), 0))).scalar_one()) + 1
    for cfg in db.execute(select(AiConfig).where(AiConfig.active.is_(True))).scalars():
        cfg.active = False
    db.add(
        AiConfig(
            version=next_version,
            system_prompt=system_prompt,
            temperature=temp,
            top_k=topk,
            active=True,
        )
    )
    db.commit()
    return RedirectResponse("/admin/ai-config", status_code=status.HTTP_303_SEE_OTHER)


# --- Conversations (oversight / safety) ------------------------------------

@router.get("/conversations", response_class=HTMLResponse)
def conversations_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    convs = list(
        db.execute(select(Conversation).order_by(Conversation.updated_at.desc()).limit(100)).scalars()
    )
    rows = []
    for c in convs:
        owner = db.get(User, c.user_id)
        count = db.execute(
            select(func.count()).select_from(Message).where(Message.conversation_id == c.id)
        ).scalar_one()
        rows.append({"conv": c, "email": owner.email if owner else "—", "count": count})
    return templates.TemplateResponse(
        "admin/conversations.html", {"request": request, "user": user, "rows": rows}
    )


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
def conversation_view(
    request: Request, conv_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    conv = db.get(Conversation, conv_id)
    if conv is None:
        return RedirectResponse("/admin/conversations", status_code=status.HTTP_303_SEE_OTHER)
    msgs = list(
        db.execute(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)).scalars()
    )
    return templates.TemplateResponse(
        "admin/conversation.html",
        {"request": request, "user": user, "conv": conv, "messages": msgs, "owner": db.get(User, conv.user_id)},
    )


# --- Revenue ----------------------------------------------------------------

@router.get("/revenue", response_class=HTMLResponse)
def revenue_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    active_by_tier = {}
    for tier in ("abhyasi", "sadhaka"):
        active_by_tier[tier] = db.execute(
            select(func.count()).select_from(Subscription).where(
                Subscription.tier == tier, Subscription.status == "active"
            )
        ).scalar_one()
    mrr = active_by_tier["abhyasi"] * 499 + active_by_tier["sadhaka"] * 1459
    total_paid = db.execute(select(func.coalesce(func.sum(Payment.amount), 0))).scalar_one()
    pays = list(db.execute(select(Payment).order_by(Payment.id.desc()).limit(50)).scalars())
    rows = [{"pay": p, "email": (db.get(User, p.user_id).email if db.get(User, p.user_id) else "—")} for p in pays]
    return templates.TemplateResponse(
        "admin/revenue.html",
        {
            "request": request,
            "user": user,
            "active_by_tier": active_by_tier,
            "mrr": mrr,
            "total_paid": total_paid,
            "rows": rows,
        },
    )


# --- Contacts ---------------------------------------------------------------

@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = list(db.execute(select(Contact).order_by(Contact.id.desc()).limit(300)).scalars())
    return templates.TemplateResponse(
        "admin/contacts.html", {"request": request, "user": user, "rows": rows}
    )
