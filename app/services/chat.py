"""The AI Mentor chat core (System B) — hybrid retrieval + tier-gated Claude.

Flow per user message (Section 5):
  query → embed → tier-gated vector search over kb_chunks → gather cited verses
  → assemble the cached system prompt with runtime variables → call Claude →
  log a generations row → deterministic verse-accuracy check.

Everything degrades safely: missing keys raise a clear RuntimeError the endpoint
turns into a friendly message; nothing crashes the app.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiConfig,
    Conversation,
    Generation,
    Message,
    UsageCounter,
    Verse,
)
from app.models.corpus import tier_level
from app.services import ai_settings, embeddings
from app.services.retrieval import search_chunks

logger = logging.getLogger("app.chat")

# Daily message caps per tier (Seeker is honestly capped; paid is effectively open).
TIER_DAILY_CAP = {"seeker": 8, "abhyasi": 200, "sadhaka": 1000}
_HISTORY_TURNS = 12  # recent messages from the current conversation sent to Claude
_VERSE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,3})\b")

# Bundled system prompt (v3.1). The active ai_config row overrides this if present.
DEFAULT_SYSTEM_PROMPT = """You are the AI Gita Mentor — the digital extension of Ganga Narayan Das (GND). You speak in his voice and from his framework. You are not a generic AI assistant, a chatbot, or a search engine. You are a mentor whose entire value is that you say what a generic AI cannot.

Ganga Narayan Das is a Ceramic Engineer (NIT Rourkela) and the founder of the Neuro-Acoustic Protocol. He became a monk in April 2008 and has implemented the protocol for 17 years across demanding environments (KPMG, NPCIL, Tata Steel, PwC, HDFC leaders, UNEA 4 & 6). He is a monk who never left the world. The programme is Sthira; the mission is the Unshakeable Generation. Never recite this biography unprompted; it shapes your authority, it is not a script.

YOUR LENS — what makes you different. You read every problem through one frame: the nervous system, mapped onto the Gita.
- SNS — the Combat Engine. High-Beta dominance; threat-detection runs continuously, cortisol elevates, the prefrontal cortex is suppressed, clarity is unavailable. The Gita named this Rajas dominance / Chitta Vritti.
- PNS — the Recovery Core. Alpha/Theta; cortisol drops, HRV rises, the prefrontal cortex comes online, presence returns. The Gita named this Sthira.
The Gita is used here as an engineering manual, not a religious text. Never proselytize; never assume the user is Hindu. The recurring pattern you name is the Sovereign's Desert: high external success with an inner core that has quietly collapsed — permanent SNS dominance, the all-clear that never arrived.

HOW YOU ANSWER — every substantial reply does four things:
1. Name the mechanism, precisely — the specific nervous-system event and its Gita correlate. You diagnose; a generic LLM only explains.
2. Make it about THIS person — use their age ({user_age}), profession ({user_profession}), gender ({user_gender}), assessment band ({assessment_band}), what they've said ({conversation_memory}), and their message.
3. Give exactly one concrete first step — one precise, do-able practice drawn from {retrieved_context}. Prescription, not a menu.
4. Reframe and open continuity — a short identity reframe and a forward hook ("sit with this a few days; tell me what shifts").
Default to depth and specificity over hedging. Never retreat into "it depends / consult a professional / ten general tips". Be warm, precise, grounded; engineering calm, not sentimentality.

PERSONALIZATION — your signature move. On a user's very first contact you ask for their age, profession, and gender, and invite them to elaborate their situation, BEFORE giving a full answer — no generic AI does this, and it is how you make the guidance fit their actual life. (The app handles that first turn for you and stores their answers.) Once you know them, weave their age, profession, and gender concretely into the diagnosis and the prescribed step — you speak to a 42-year-old founder differently than to a 24-year-old student.

VERSE DISCIPLINE — non-negotiable. Cite a specific verse ONLY when it appears in {retrieved_context} with its canonical text. Never invent, renumber, or paraphrase-from-memory a Gita verse. If no verse was retrieved, speak to the principle without a citation rather than risk a wrong one.

USE ONLY WHAT YOU'RE GIVEN. Your depth comes from {retrieved_context} and {conversation_memory}. Do not fabricate GND's track record, testimonials, figures, course names, or prices beyond what is retrieved or stated here.

TIERS — current user is {user_tier}.
- Seeker (free): give genuine recognition, the precise mechanism, and one real first step — a true taste. Then draw an honest boundary: the continuation of this practice, and memory of their journey, live in Abhyāsi. Upgrade by continuity, never by withholding the truth of their situation.
- Abhyāsi (₹499/mo): full working practice for their domain, with memory across sessions.
- Sādhaka (₹2,499/mo): the deepest protocol plus the video library. When a small domain is clear you may prescribe one video by naming its id (e.g. `prescribe-video: anger_01`) — you never generate the link yourself. If the issue persists, offer the ₹5,000 personal call (gateway to the personal Sthira programme).

PRESCRIPTION & ESCALATION. You are the explorer lane. Ladder: Seeker → Abhyāsi → Sādhaka → prescribed videos → ₹5,000 personal call → personal Sthira programme. Move someone up only one rung at a time, only when the moment is right. Never present a catalogue. Prescribe one thing, like a physician who has just diagnosed.

SAFETY — overrides everything. If the user signals self-harm, suicidal thoughts, abuse, acute crisis, or danger: stop the frame entirely. Do not diagnose a mechanism, do not prescribe, do not upsell. Respond as a caring human, take them seriously, gently encourage them to reach a trusted person or a professional/crisis line, and stay supportive. Never provide methods or anything that could enable harm. You are guidance rooted in the Gita and the Neuro-Acoustic Protocol — not a therapist, doctor, lawyer, or financial advisor. Users are 18+.

RUNTIME CONTEXT
- User: {user_name} · Tier: {user_tier} · Age: {user_age} · Profession: {user_profession} · Gender: {user_gender} · Assessment band: {assessment_band}
- Retrieved knowledge for this turn: {retrieved_context}
- What you remember about them: {conversation_memory}

Answer as the AI Gita Mentor."""


# The fixed first-contact message — the personalization differentiator. The app
# sends this as the mentor's first reply, then stores the user's profile.
ONBOARDING_PROMPT = (
    "Before I answer — and so this is genuinely *yours*, not a generic reply — may I know a little "
    "about you: your **age**, your **profession**, and your **gender**?\n\n"
    "And if you can say a bit more about the situation you're in, I can be far more exact — and point "
    "you to the real solution, not a general one.\n\n"
    "*(No other AI asks you this first. It's how I make the guidance fit your actual life.)*"
)


def get_system_prompt(db: Session) -> str:
    row = db.execute(
        select(AiConfig).where(AiConfig.active.is_(True)).order_by(AiConfig.version.desc())
    ).scalars().first()
    return row.system_prompt if row and row.system_prompt else DEFAULT_SYSTEM_PROMPT


def extract_profile(db: Session, text: str) -> dict:
    """Pull {age, profession, gender} from the user's free-text profile reply.

    Best-effort: returns {} if no key or on any failure. Never raises."""
    import json

    cfg = ai_settings.resolved(db)
    key = cfg.key_for("anthropic")
    if not key:
        return {}
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=cfg.chat_model,
            max_tokens=200,
            system=(
                "Extract the user's age, profession, and gender from their message. Reply with ONLY a "
                'JSON object: {"age": <integer or null>, "profession": <string or null>, '
                '"gender": <string or null>}. Use null for anything not clearly stated.'
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        data = json.loads(match.group(0))
        try:
            age = int(data.get("age")) if data.get("age") is not None else None
        except (TypeError, ValueError):
            age = None
        prof = data.get("profession")
        gender = data.get("gender")
        return {
            "age": age,
            "profession": (str(prof).strip()[:160] if prof else None),
            "gender": (str(gender).strip()[:40] if gender else None),
        }
    except Exception:  # noqa: BLE001
        logger.warning("Profile extraction failed", exc_info=True)
        return {}


# --- rate limiting ----------------------------------------------------------

def check_and_increment_usage(db: Session, user) -> tuple[bool, int, int]:
    """Returns (allowed, count_after, cap). Increments only when allowed."""
    today = dt.date.today()
    counter = db.execute(
        select(UsageCounter).where(
            UsageCounter.user_id == user.id, UsageCounter.period_date == today
        )
    ).scalars().first()
    if counter is None:
        counter = UsageCounter(user_id=user.id, period_date=today, message_count=0)
        db.add(counter)
        db.flush()
    cap = TIER_DAILY_CAP.get(user.tier, TIER_DAILY_CAP["seeker"])
    if counter.message_count >= cap:
        return False, counter.message_count, cap
    counter.message_count += 1
    db.commit()
    return True, counter.message_count, cap


# --- retrieval + memory -----------------------------------------------------

def _format_context(db: Session, chunks: list[dict]) -> tuple[str, list[int], list[str], float]:
    if not chunks:
        return ("No specific recorded material was retrieved for this turn.", [], [], 0.0)
    chunk_ids = [c["id"] for c in chunks]
    verse_refs: list[str] = []
    parts: list[str] = []
    for c in chunks:
        attr = c.get("attribution") or {}
        ref = attr.get("verse_ref")
        tag = f"[source:{attr.get('origin', 'manual')}" + (f" · verse {ref}" if ref else "") + "]"
        if ref and ref not in verse_refs:
            verse_refs.append(ref)
        parts.append(f"{tag}\n{c['chunk_text']}")
    # Append canonical verse text for any cited verse, so citations stay accurate.
    if verse_refs:
        verses = db.execute(select(Verse).where(Verse.verse_ref.in_(verse_refs))).scalars().all()
        for v in verses:
            line = f"[canonical verse {v.verse_ref}] {v.translation or ''}".strip()
            if v.sanskrit:
                line += f" — {v.sanskrit}"
            parts.append(line)
    distances = [c["distance"] for c in chunks if c.get("distance") is not None]
    score = round(1.0 - (sum(distances) / len(distances)), 4) if distances else 0.0
    return ("\n\n".join(parts), chunk_ids, verse_refs, score)


def _build_memory(db: Session, user, conversation: Conversation) -> str:
    if user.tier == "seeker":
        return "This is a free-tier session; you do not retain memory between sessions for this user. Memory begins at Abhyāsi."
    # Paid: brief recap from the user's other recent conversations.
    rows = db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.user_id == user.id,
            Conversation.id != conversation.id,
            Message.role == "user",
        )
        .order_by(Message.id.desc())
        .limit(6)
    ).scalars().all()
    if not rows:
        return "No prior sessions yet — this is the beginning of their journey with you."
    bullets = "; ".join(m.content[:160] for m in reversed(rows))
    return f"Recent things this user has raised in past sessions: {bullets}"


def _history_messages(db: Session, conversation: Conversation) -> list[dict]:
    rows = db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role.in_(("user", "assistant")))
        .order_by(Message.id.desc())
        .limit(_HISTORY_TURNS)
    ).scalars().all()
    msgs = [{"role": m.role, "content": m.content} for m in reversed(rows)]
    while msgs and msgs[0]["role"] != "user":  # Claude requires the first turn to be 'user'
        msgs.pop(0)
    return msgs


# --- the Claude call --------------------------------------------------------

def _call_claude(system: str, messages: list[dict], cfg) -> tuple[str, int, int]:
    key = cfg.key_for("anthropic")
    if not key:
        raise RuntimeError("Anthropic API key not configured — set it in Settings → AI.")
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=cfg.chat_model,
        max_tokens=1024,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    usage = getattr(resp, "usage", None)
    return text, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0


def _verse_accuracy(answer: str, allowed_refs: list[str]) -> bool | None:
    """Deterministic check: every verse ref cited must have been retrieved.

    None if the answer cites no verse (nothing to check)."""
    cited = {f"{m.group(1)}.{m.group(2)}" for m in _VERSE_RE.finditer(answer)}
    if not cited:
        return None
    return cited.issubset(set(allowed_refs))


def _fill(template: str, values: dict) -> str:
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val if val is not None else ""))
    return out


def answer(db: Session, user, conversation: Conversation, user_message: str) -> tuple[Generation, str]:
    """Produce one grounded, tier-gated answer; log a generations row."""
    cfg = ai_settings.resolved(db)

    # 1) embed + tier-gated retrieval
    chunk_ids: list[int] = []
    verse_refs: list[str] = []
    context = "No recorded material was retrieved."
    score = 0.0
    query_emb = None
    try:
        query_emb = embeddings.embed_query(user_message, cfg.key_for("openai"), cfg.embedding_model)
        chunks = search_chunks(db, query_emb, user.tier, k=8)
        context, chunk_ids, verse_refs, score = _format_context(db, chunks)
    except Exception as exc:  # noqa: BLE001 — retrieval is best-effort
        logger.warning("Retrieval failed (continuing without context): %s", exc)

    # 2) assemble prompt
    system = _fill(
        get_system_prompt(db),
        {
            "user_name": user.name or "friend",
            "user_tier": user.tier,
            "user_age": user.age or "unknown",
            "user_profession": user.profession or "unknown",
            "user_gender": user.gender or "unknown",
            "assessment_band": user.assessment_band or "unknown",
            "retrieved_context": context,
            "conversation_memory": _build_memory(db, user, conversation),
        },
    )
    # History already includes the just-saved user message (the caller persists it
    # before calling answer), so it goes straight to Claude — no double-append.
    history = _history_messages(db, conversation)

    # 3) call Claude (timed)
    started = time.perf_counter()
    answer_text, tin, tout = _call_claude(system, history, cfg)
    latency_ms = int((time.perf_counter() - started) * 1000)

    # 4) deterministic verse check + log
    verse_ok = _verse_accuracy(answer_text, verse_refs)
    gen = Generation(
        user_id=user.id,
        conversation_id=conversation.id,
        question_text=user_message,
        query_embedding=query_emb,
        retrieved_chunk_ids={"ids": chunk_ids},
        final_prompt=system[:8000],
        final_answer=answer_text,
        model=cfg.chat_model,
        tokens_in=tin,
        tokens_out=tout,
        latency_ms=latency_ms,
        retrieval_score=score,
        verse_accuracy=verse_ok,
        feedback={},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen, answer_text
