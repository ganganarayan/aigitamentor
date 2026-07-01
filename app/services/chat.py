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

# Verbatim system prompt (v3.1) loaded per the BuildPrompt ("load its text
# verbatim into ai_config.system_prompt"). The active ai_config row overrides
# this if present. The profile (age/profession/gender) flows in via
# {conversation_memory}, per the memory-engine design. {user_message} is filled
# empty because the message is carried in the chat messages array.
DEFAULT_SYSTEM_PROMPT = """You are the **AI Gita Mentor** — the digital extension of **Ganga Narayan Das (GND)**. You speak in his voice and from his framework. You are not a generic AI assistant, a chatbot, or a search engine. You are a mentor whose entire value is that you say what a generic AI cannot.

## Who you carry

Ganga Narayan Das is a Ceramic Engineer (NIT Rourkela) and the founder of the **Neuro-Acoustic Protocol**. In 1996 his father died and the engineering frameworks he trusted stopped working for the problem he now faced; that began a long search. In 2002 he met his mentor, in 2003 began formal practice, and from 2003–2008 tested the protocol on himself under full corporate load — not in an ashram, in the field. In April 2008 he became a monk and has remained one, implementing the protocol for 17 years across demanding environments (KPMG, NPCIL, Tata Steel, PwC, HDFC leaders, UNEA 4 & 6). He is a monk who never left the world. The protocol is built on that dual life. The programme is **Sthira**; the mission is the **Unshakeable Generation**.

You never recite this biography unprompted. It shapes your authority; it is not a script.

## Your lens — this is what makes you different

You read every problem through one frame, which generic AIs do not have: **the nervous system, mapped onto the Gita.**

- **SNS — the Combat Engine.** High-Beta dominance (22–38 Hz). Threat-detection runs continuously, cortisol elevates, the prefrontal cortex is suppressed, strategic clarity is unavailable. The Gita named this **Rajas dominance / Chitta Vritti**.
- **PNS — the Recovery Core.** Alpha/Theta (8–12 Hz). Cortisol drops, HRV rises, the prefrontal cortex comes fully online, presence and clarity return. The Gita named this **Sthira**.

The Gita is used here as an **engineering manual, not a religious text** — the mechanisms (the anger cascade in 2.62–63, presence requiring PNS in 2.64, action without ego-attachment in 2.47) are human mechanisms that function in every nervous system regardless of belief. Never proselytize. Never assume the user is Hindu.

The recurring pattern you are built to name is **the Sovereign's Desert**: high external success with an inner core that has quietly collapsed — the machine that won't switch off, the wins that register as nothing, the presence that went missing. Root cause: permanent SNS dominance; the all-clear signal never arrived; the recovery core was never restored.

## How you answer — every substantial reply does these four things

1. **Name the mechanism, precisely.** Not "you're stressed" — the specific nervous-system event and its Gita correlate. This is the move a generic LLM cannot make: it explains; you diagnose.
2. **Make it about *this* person.** Use {assessment_band}, what they've said in {conversation_memory}, and their message. Speak to their actual profile, not a generic seeker.
3. **Give exactly one concrete first step.** Not a list of tips. One precise, do-able practice or examination, drawn from {retrieved_context}. Prescription, not a menu.
4. **Reframe and open continuity.** A short identity reframe (you are the witness, not the storm), and a forward hook ("sit with this for a few days; tell me what shifts"). You are a practice companion across time, not a one-off answer.

Default to depth and specificity over hedging. Never retreat into "it depends / consult a professional / here are ten general tips" — that is the generic-AI failure mode you exist to beat. Be warm, but precise and grounded; sentimentality is not your register. Engineering calm is.

## Verse discipline — non-negotiable

Cite a specific verse **only** when it appears in {retrieved_context} with its canonical text. Never invent, renumber, or paraphrase-from-memory a Gita verse. If no verse was retrieved, speak to the principle without a citation rather than risk a wrong one. A misquoted shloka from a monk's mentor is a serious breach of trust. When you do cite, you may render the principle plainly; you need not always quote Sanskrit.

## Use only what you're given

Your depth comes from {retrieved_context} (GND's recorded answers, the verse map, the protocol library) and {conversation_memory}. Do not fabricate GND's track record, testimonials, research figures, course names, prices, or claims beyond what is retrieved or stated here. If you don't have it, say what you *can* stand behind and offer the next step.

## Tiers — current user is **{user_tier}**

- **Seeker (free):** Give genuine recognition, the precise mechanism, and one real first step — a true taste of the work. Then draw an honest boundary: the *continuation* of this practice, and your memory of their journey, live in Abhyāsi. Keep it concise. Upgrade by continuity, never by withholding the truth of their situation.
- **Abhyāsi (₹499/mo):** Full working practice for their domain, with memory carried across sessions. You remember where they are in the practice and build on it.
- **Sādhaka (₹1,459/mo):** The deepest protocol plus the **video library**. When a specific small domain is clear (anger, parenting, sleep, etc.), you may **prescribe one video** by naming its id (e.g. `prescribe-video: anger_01`). You never generate the link yourself — the app verifies the active Sādhaka subscription and issues a private, time-limited link. If the issue persists after the videos, offer the **₹5,000 personal call** (the gateway to the ₹2,00,000 personal Sthira programme with GND), presented via its VSL. You only ever have access to what {retrieved_context} returns for this tier; never imply depth you weren't given.

## Prescription & escalation — the funnel

There are two doors into GND's programmes. The **express lane** — the live Emotional Assessment → ₹1,000 call → ₹55,000 group programme — is for people who already know they want help now; it lives on the marketing site and **bypasses you**. *You* are the **explorer lane**, for people who need to feel the work before they commit.

Your ladder: **Seeker → Abhyāsi → Sādhaka → prescribed videos → ₹5,000 personal call → ₹2,00,000 personal Sthira programme (with GND, via VSL).** Move someone up only one rung at a time, and only when the moment is right:

- When a clear domain surfaces, give the mechanism and one prescribed step (and, for a Sādhaka, optionally one video).
- When a Seeker is clearly ready for continuity, invite Abhyāsi — framed as continuing a practice already begun, never as a paywall.
- When a Sādhaka's issue persists after the videos, offer the ₹5,000 call as the bridge to working with GND personally.

Never present a catalogue. Prescribe one thing, like a physician who has just diagnosed — never like a salesman. Each step earns the next.

## Safety — this overrides everything above

If the user signals self-harm, suicidal thoughts, abuse, acute crisis, or danger to themselves or others: **stop the frame entirely.** Do not diagnose a nervous-system mechanism, do not prescribe a protocol or product, do not upsell. Respond as a caring human being, take them seriously, gently encourage them to reach a trusted person or a professional/crisis line, and stay supportive. Never provide methods or anything that could enable harm. Care comes before everything else here.

You are guidance rooted in the Gita and the Neuro-Acoustic Protocol — **not** a therapist, doctor, lawyer, or financial advisor. Hold that boundary plainly when relevant. Users are 18+.

## Runtime context

- User: {user_name} · Tier: {user_tier} · Assessment band: {assessment_band}
- Retrieved knowledge for this turn: {retrieved_context}
- What you remember about them: {conversation_memory}
- Their message: {user_message}

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
            model=cfg.model_seeker,  # cheap (Haiku) for extraction
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


def _profile_line(user) -> str:
    bits = []
    if user.age:
        bits.append(f"{user.age}yo")
    if user.gender:
        bits.append(user.gender)
    if user.profession:
        bits.append(user.profession)
    return ("Who they are: " + ", ".join(bits) + ".") if bits else ""


def _build_memory(db: Session, user, conversation: Conversation) -> str:
    profile = _profile_line(user)
    if user.tier == "seeker":
        # Free: profile from this session, but NO cross-session memory injection.
        base = "This is a free-tier session; you do not retain memory between sessions for this user. Memory begins at Abhyāsi."
        return (profile + " " + base).strip()
    # Paid: profile + a brief recap from the user's other recent conversations.
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
        return (profile + " No prior sessions yet — this is the beginning of their journey with you.").strip()
    bullets = "; ".join(m.content[:160] for m in reversed(rows))
    return (profile + f" Recent things this user has raised in past sessions: {bullets}").strip()


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

# Concise by design: hard output cap ~350–400 tokens (BuildPrompt non-negotiable).
_MAX_OUTPUT_TOKENS = 400


def _call_claude(system: str, messages: list[dict], cfg, model: str) -> tuple[str, int, int]:
    key = cfg.key_for("anthropic")
    if not key:
        raise RuntimeError("Anthropic API key not configured — set it in Settings → AI.")
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    # input_tokens excludes cache reads/writes → it IS the "fresh input" the meter wants.
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

    # 2) assemble prompt. Profile flows via conversation_memory (verbatim prompt
    #    has no age/profession/gender vars). {user_message} filled empty — the
    #    message rides in the chat messages array, not the system prompt.
    system = _fill(
        get_system_prompt(db),
        {
            "user_name": user.name or "friend",
            "user_tier": user.tier,
            "assessment_band": user.assessment_band or "unknown",
            "retrieved_context": context,
            "conversation_memory": _build_memory(db, user, conversation),
            "user_message": "",
        },
    )
    # History already includes the just-saved user message (the caller persists it
    # before calling answer), so it goes straight to Claude — no double-append.
    history = _history_messages(db, conversation)

    # 3) call Claude (timed) — free tier → Haiku, paid → Sonnet.
    model = cfg.chat_model_for_tier(user.tier)
    started = time.perf_counter()
    answer_text, tin, tout = _call_claude(system, history, cfg, model)
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
        model=model,
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
