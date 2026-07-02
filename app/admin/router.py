"""Admin routes — recorder, baseline panel, recordings, publishing, seed."""

from __future__ import annotations

import datetime as dt

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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth.deps import require_admin
from app.config import settings
from app.db import get_db
from app.models import (
    AiConfig,
    Contact,
    Conversation,
    Event,
    Generation,
    KbAnswer,
    KbChunk,
    KbSource,
    LlmBaseline,
    Message,
    Payment,
    PublicKbArticle,
    Question,
    Recording,
    ResourceGrant,
    Subscription,
    User,
    Verse,
    VideoResource,
)
from app.models.corpus import TIER_RANK
from app.services import ai_settings
from app.services import chat as chat_service
from app.services import ingestion
from app.services import llm_baselines
from app.services import public_kb
from app.services import secretbox
from app.services import settings_store
from app.services import site_settings
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


STAGES = ["", "recognition", "mechanism", "first-regulation", "deeper-practice", "integration"]


def _save_typed_answer(
    db: Session, question_id: int, tier: str, text: str, user: User,
    stage: str | None = None, publish: bool = True,
) -> KbAnswer:
    """Upsert a typed/pasted/dictated answer for (question, tier).

    The typed text IS the transcript — same field a transcription would fill —
    so it flows through the identical publish → ingest pipeline. Idempotent per
    (question, tier): re-saving updates in place rather than duplicating.

    ``publish=False`` (autosave-as-you-type) stores the text but leaves the status
    a draft — new rows start as draft; an already-published row is NOT downgraded
    and its version is not bumped. Explicit save publishes.
    """
    if tier not in TIER_RANK:
        tier = "seeker"
    stage = stage if stage in STAGES and stage else None
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
            stage=stage,
            transcript_raw=text,
            transcript_edited=text,
            answer_final=text,
            status="published" if publish else "draft",
            version=1,
        )
        db.add(answer)
    else:
        answer.transcript_edited = text
        answer.answer_final = text
        answer.stage = stage
        if publish:
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


_TIERS = ["seeker", "abhyasi", "sadhaka"]


@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = list(db.execute(select(User).order_by(User.id.desc()).limit(300)).scalars())
    cfg = ai_settings.resolved(db)
    rows = []
    for u in users:
        tokens = db.execute(
            select(
                func.coalesce(
                    func.sum(func.coalesce(Generation.tokens_in, 0) + func.coalesce(Generation.tokens_out, 0)),
                    0,
                )
            ).where(Generation.user_id == u.id)
        ).scalar_one()
        amount = db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.user_id == u.id, Payment.status.in_(("captured", "paid"))
            )
        ).scalar_one()
        rows.append(
            {"u": u, "tokens": int(tokens or 0), "amount": float(amount or 0), "model": cfg.chat_model_for_tier(u.tier)}
        )
    return templates.TemplateResponse(
        "admin/users.html", {"request": request, "user": user, "rows": rows}
    )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def user_edit_page(
    request: Request, user_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "admin/user_edit.html",
        {"request": request, "user": user, "target": target, "tiers": _TIERS, "roles": ["user", "admin"]},
    )


@router.post("/users/{user_id}/edit")
def user_edit_save(
    user_id: int,
    tier: str = Form("seeker"),
    role: str = Form("user"),
    plan_expires: str = Form(""),
    assessment_taken: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)
    if tier in _TIERS:
        target.tier = tier
    if role in ("user", "admin"):
        target.role = role
    plan_expires = plan_expires.strip()
    if plan_expires:
        try:
            d = dt.date.fromisoformat(plan_expires)
            target.plan_expires_at = dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    else:
        target.plan_expires_at = None
    assessment_taken = assessment_taken.strip()
    if assessment_taken:
        try:
            d = dt.date.fromisoformat(assessment_taken)
            target.assessment_taken_at = dt.datetime(d.year, d.month, d.day, 12, 0, tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    else:
        target.assessment_taken_at = None
    db.commit()
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/recorder/questions")
def add_question(
    question_text: str = Form(...),
    domain: str = Form(""),
    gita_reference: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Add a question on the spot, then open its recorder page to answer it now."""
    qt = (question_text or "").strip()
    if not qt:
        return RedirectResponse("/admin/recorder", status_code=status.HTTP_303_SEE_OTHER)
    q = Question(
        question_text=qt,
        domain=(domain.strip() or None),
        gita_reference=(gita_reference.strip() or None),
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return RedirectResponse(f"/admin/recorder/{q.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/recorder/{question_id}/autosave", response_class=JSONResponse)
def autosave_answer(
    question_id: int,
    text: str = Form(""),
    tier: str = Form("seeker"),
    stage: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Continuously persist the answer as it's typed/dictated — kept as a DRAFT so
    an in-progress answer never enters the corpus until explicitly published."""
    text = (text or "").strip()
    if not text:
        return JSONResponse({"ok": True, "empty": True})
    ans = _save_typed_answer(db, question_id, tier, text, user, stage=stage, publish=False)
    return JSONResponse({"ok": True, "status": ans.status})


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
            "stages": STAGES,
            "done_tiers": done,
        },
    )


@router.get("/recorder/{question_id}/baselines/list", response_class=JSONResponse)
def baselines_list(question_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = llm_baselines.baselines_for(db, question_id)
    return JSONResponse({r.provider: (r.answer_text or "") for r in rows})


@router.post("/recorder/{question_id}/baselines/generate", response_class=JSONResponse)
def baselines_generate(question_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    question = db.get(Question, question_id)
    if question is None:
        return JSONResponse({}, status_code=404)
    rows = llm_baselines.generate_baselines(db, question)
    return JSONResponse({r.provider: (r.answer_text or "") for r in rows})


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
    stage: str = Form(""),
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
        _save_typed_answer(db, question_id, tier, text, user, stage=stage)
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
            "stages": STAGES,
            "chunks": ingestion.chunk_count_for(db, answer.id),
        },
    )


@router.post("/answers/{answer_id}/edit")
def answer_edit_save(
    answer_id: int,
    background: BackgroundTasks,
    text: str = Form(...),
    tier: str = Form("seeker"),
    stage: str = Form(""),
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
    answer.stage = stage if (stage in STAGES and stage) else None
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
    ai = ai_settings.view(db)
    # Live model lists so the dropdowns render populated (only models the provider
    # actually offers). Fetch each underlying provider once (claude ≡ anthropic);
    # cheap when a key is unset (returns [] / a public fallback without an HTTP call).
    cache: dict[str, list[str]] = {}

    def models_for(name: str) -> list[str]:
        key = ai_settings.BASELINE_TO_KEY_PROVIDER.get(name, name)
        if key not in cache:
            cache[key] = ai_settings.list_provider_models(db, key)
        return cache[key]

    models = models_for(ai["provider"])
    baseline_model_options = {p: models_for(p) for p in ai["baseline_choices"]}
    return templates.TemplateResponse(
        "admin/settings.html",
        {"request": request, "user": user, "ai": ai, "models": models,
         "baseline_model_options": baseline_model_options, "error": error},
    )


@router.get("/models", response_class=JSONResponse)
def admin_models(
    provider: str = "anthropic",
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Live list of available model ids for a provider (populates the pickers)."""
    return JSONResponse({"models": ai_settings.list_provider_models(db, provider)})


@router.post("/settings")
def settings_save(
    provider: str = Form("anthropic"),
    model_admin: str = Form(""),
    model_seeker: str = Form(""),
    model_abhyasi: str = Form(""),
    model_sadhaka: str = Form(""),
    embedding_model: str = Form(""),
    transcribe_model: str = Form(""),
    baseline_providers: list[str] = Form(default=[]),
    bmodel_claude: str = Form(""),
    bmodel_openai: str = Form(""),
    bmodel_gemini: str = Form(""),
    bmodel_perplexity: str = Form(""),
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
    key_updates = {
        "anthropic": anthropic_api_key.strip(),
        "openai": openai_api_key.strip(),
        "gemini": gemini_api_key.strip(),
        "perplexity": perplexity_api_key.strip(),
    }
    key_clears = [
        prov
        for prov, flag in [
            ("anthropic", clear_anthropic),
            ("openai", clear_openai),
            ("gemini", clear_gemini),
            ("perplexity", clear_perplexity),
        ]
        if flag
    ]
    models = [model_admin.strip(), model_seeker.strip(), model_abhyasi.strip(), model_sadhaka.strip()]
    ai_settings.update(
        db,
        provider=provider.strip(),
        model_admin=model_admin.strip(),
        model_seeker=model_seeker.strip(),
        model_abhyasi=model_abhyasi.strip(),
        model_sadhaka=model_sadhaka.strip(),
        embedding_model=embedding_model.strip(),
        transcribe_model=transcribe_model.strip(),
        baseline_providers=baseline_providers,
        baseline_models={
            "claude": bmodel_claude, "openai": bmodel_openai,
            "gemini": bmodel_gemini, "perplexity": bmodel_perplexity,
        },
        key_updates=key_updates,
        key_clears=key_clears,
    )
    error = ai_settings.validate_chat_models(db, models)
    if error:
        return RedirectResponse(f"/admin/settings?error={error}", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/admin/settings", status_code=status.HTTP_303_SEE_OTHER)


# --- Integrations (DB-backed env: Google, Razorpay, Meta, Video/CDN) --------

@router.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request, error: str = "",
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/integrations.html",
        {
            "request": request, "user": user, "error": error,
            "sections": settings_store.sections(db),
            "audit": settings_store.recent_audit(db, 30),
            "dedicated_key": secretbox.using_dedicated_key(),
        },
    )


@router.post("/integrations")
async def integrations_save(
    request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    form = await request.form()
    updates: dict[str, str] = {}
    clears: list[str] = []
    for f in settings_store.FIELDS:
        updates[f.name] = str(form.get(f.name, "") or "")
        if form.get("clear_" + f.name):
            clears.append(f.name)
    settings_store.save(db, updates, clears, actor_id=user.id, actor_email=user.email)
    return RedirectResponse("/admin/integrations", status_code=status.HTTP_303_SEE_OTHER)


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


# --- Safety Logs ------------------------------------------------------------

@router.get("/safety", response_class=HTMLResponse)
def safety_logs(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    events = list(
        db.execute(
            select(Event).where(Event.event_name == "safety_flag").order_by(Event.id.desc()).limit(200)
        ).scalars()
    )
    rows = [
        {
            "event": e,
            "email": (db.get(User, e.user_id).email if e.user_id and db.get(User, e.user_id) else "—"),
        }
        for e in events
    ]
    return templates.TemplateResponse(
        "admin/safety.html", {"request": request, "user": user, "rows": rows}
    )


# --- Video resources (Chunk 5): the anger-escalation library ------------------

@router.get("/videos", response_class=HTMLResponse)
def videos_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    videos = list(db.execute(select(VideoResource).order_by(VideoResource.topic, VideoResource.id.desc())).scalars())
    grant_count = db.execute(select(func.count()).select_from(ResourceGrant)).scalar_one()
    return templates.TemplateResponse(
        "admin/videos.html",
        {"request": request, "user": user, "videos": videos, "grant_count": grant_count},
    )


@router.post("/videos")
def videos_create(
    topic: str = Form(...),
    title: str = Form(...),
    embed_html: str = Form(...),
    note: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    topic = topic.strip().lower()
    if topic and title.strip() and embed_html.strip():
        db.add(
            VideoResource(
                topic=topic, title=title.strip(), embed_html=embed_html.strip(),
                note=note.strip() or None, active=True,
            )
        )
        db.commit()
    return RedirectResponse("/admin/videos", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/videos/{video_id}/edit")
def videos_edit(
    video_id: int,
    topic: str = Form(...),
    title: str = Form(...),
    embed_html: str = Form(...),
    note: str = Form(""),
    active: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    v = db.get(VideoResource, video_id)
    if v is not None:
        v.topic = topic.strip().lower() or v.topic
        v.title = title.strip() or v.title
        v.embed_html = embed_html.strip() or v.embed_html
        v.note = note.strip() or None
        v.active = active == "on"
        db.commit()
    return RedirectResponse("/admin/videos", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/videos/{video_id}/delete")
def videos_delete(video_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    v = db.get(VideoResource, video_id)
    if v is not None:
        db.delete(v)
        db.commit()
    return RedirectResponse("/admin/videos", status_code=status.HTTP_303_SEE_OTHER)


# --- Escalation settings (Chunk 5): the 1-on-1 + assessment links -------------

@router.get("/escalation", response_class=HTMLResponse)
def escalation_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    cfg = site_settings.get_escalation(db)
    videos = db.execute(select(func.count()).select_from(VideoResource).where(VideoResource.active.is_(True))).scalar_one()
    return templates.TemplateResponse(
        "admin/escalation.html",
        {"request": request, "user": user, "cfg": cfg, "active_videos": videos},
    )


@router.post("/escalation")
def escalation_save(
    booking_url: str = Form(""),
    assessment_url: str = Form(""),
    ttl_hours: str = Form("24"),
    fresh_days: str = Form("15"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    site_settings.save_escalation(
        db, booking_url=booking_url, assessment_url=assessment_url,
        ttl_hours=ttl_hours, fresh_days=fresh_days,
    )
    return RedirectResponse("/admin/escalation", status_code=status.HTTP_303_SEE_OTHER)


# --- Public KB (Phase 7): promote Seeker answers to the crawlable /learn ------

@router.get("/public", response_class=HTMLResponse)
def public_kb_page(
    request: Request, err: str = "", user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    articles = list(
        db.execute(select(PublicKbArticle).order_by(PublicKbArticle.id.desc())).scalars()
    )
    return templates.TemplateResponse(
        "admin/public.html",
        {"request": request, "user": user, "articles": articles,
         "base": settings.app_base_url.rstrip("/"), "err": err},
    )


@router.post("/answers/{answer_id}/publish")
def public_kb_publish(answer_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    _article, err = public_kb.publish_answer(db, answer_id)
    dest = "/admin/public"
    if err:
        from urllib.parse import quote

        dest += "?err=" + quote(err)
    return RedirectResponse(dest, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/public/{article_id}/unpublish")
def public_kb_unpublish(article_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    public_kb.unpublish(db, article_id)
    return RedirectResponse("/admin/public", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/public/{article_id}/republish")
def public_kb_republish(article_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    art = db.get(PublicKbArticle, article_id)
    if art is not None and art.source_answer_id:
        public_kb.publish_answer(db, art.source_answer_id)
    return RedirectResponse("/admin/public", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/public/{article_id}/delete")
def public_kb_delete(article_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    art = db.get(PublicKbArticle, article_id)
    if art is not None:
        db.delete(art)
        db.commit()
    return RedirectResponse("/admin/public", status_code=status.HTTP_303_SEE_OTHER)
