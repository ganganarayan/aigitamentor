"""Starter seed data — idempotent.

A small, real subset so the recorder and baseline panel are demonstrable before
the full 160-question corpus is imported. Canonical verse text lives here as the
deterministic source of truth (never transcribed). Re-running is safe: rows are
keyed by natural identifiers (verse_ref, q_number, chapter number).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chapter, Question, Relationship, Verse

_CHAPTER_2 = {
    "number": 2,
    "title": "Sankhya Yoga — The Yoga of Knowledge",
    "summary": "Krishna's core teaching: the eternal self, equanimity in action, and the mechanism of how desire becomes anger and clouds judgment.",
}

_VERSES = [
    {
        "verse_ref": "2.47",
        "sanskrit": "कर्मण्येवाधिकारस्ते मा फलेषु कदाचन। मा कर्मफलहेतुर्भूर्मा ते सङ्गोऽस्त्वकर्मणि॥",
        "transliteration": "karmaṇy-evādhikāras te mā phaleṣu kadācana / mā karma-phala-hetur bhūr mā te saṅgo 'stv akarmaṇi",
        "translation": "You have a right to action alone, never to its fruits. Let not the fruits of action be your motive, nor let your attachment be to inaction.",
        "plain_explanation": "Act fully; release the outcome. Ownership of effort, not of result — the antidote to outcome-anxiety.",
    },
    {
        "verse_ref": "2.62",
        "sanskrit": "ध्यायतो विषयान्पुंसः सङ्गस्तेषूपजायते। सङ्गात्सञ्जायते कामः कामात्क्रोधोऽभिजायते॥",
        "transliteration": "dhyāyato viṣayān puṁsaḥ saṅgas teṣūpajāyate / saṅgāt sañjāyate kāmaḥ kāmāt krodho 'bhijāyate",
        "translation": "Dwelling on objects of the senses breeds attachment; from attachment springs desire; from desire arises anger.",
        "plain_explanation": "The first half of the anger cascade — rumination → attachment → craving → anger. A precise nervous-system sequence.",
    },
    {
        "verse_ref": "2.63",
        "sanskrit": "क्रोधाद्भवति सम्मोहः सम्मोहात्स्मृतिविभ्रमः। स्मृतिभ्रंशाद् बुद्धिनाशो बुद्धिनाशात्प्रणश्यति॥",
        "transliteration": "krodhād bhavati sammohaḥ sammohāt smṛti-vibhramaḥ / smṛti-bhraṁśād buddhi-nāśo buddhi-nāśāt praṇaśyati",
        "translation": "From anger comes delusion; from delusion, loss of memory; from loss of memory, the ruin of discernment; and from that ruin, one is lost.",
        "plain_explanation": "The cascade's end: anger suppresses the prefrontal cortex — memory and judgment collapse. The mechanism, stated in 400 BCE.",
    },
    {
        "verse_ref": "2.64",
        "sanskrit": "रागद्वेषवियुक्तैस्तु विषयानिन्द्रियैश्चरन्। आत्मवश्यैर्विधेयात्मा प्रसादमधिगच्छति॥",
        "transliteration": "rāga-dveṣa-vimuktais tu viṣayān indriyaiś caran / ātma-vaśyair vidheyātmā prasādam adhigacchati",
        "translation": "But one who moves among the objects of the senses, free from attachment and aversion and master of the self, attains tranquility.",
        "plain_explanation": "Prasada — the recovery state. Presence is available when the system is regulated, not when the world is controlled.",
    },
]

# (q_number, domain, question_text, gita_reference, primary_verse_ref)
_QUESTIONS = [
    (1, "Action & Anxiety", "How do I stop obsessing over outcomes I can't control at work?", "2.47", "2.47"),
    (2, "Anger", "Why do I explode in anger even when I tell myself to stay calm?", "2.62-2.63", "2.63"),
    (3, "Burnout", "I've achieved everything I wanted but feel nothing. Why?", "2.64", "2.64"),
    (4, "Decision-Making", "My mind goes blank under pressure and I make bad calls. What's happening?", "2.63", "2.63"),
    (5, "Presence", "How do I stay present when my mind won't switch off?", "2.64", "2.64"),
]


def seed_starter(db: Session) -> dict:
    counts = {"chapters": 0, "verses": 0, "questions": 0, "relationships": 0}

    chapter = db.execute(select(Chapter).where(Chapter.number == 2)).scalar_one_or_none()
    if chapter is None:
        chapter = Chapter(**_CHAPTER_2)
        db.add(chapter)
        db.flush()
        counts["chapters"] += 1

    verses_by_ref: dict[str, Verse] = {}
    for v in _VERSES:
        existing = db.execute(select(Verse).where(Verse.verse_ref == v["verse_ref"])).scalar_one_or_none()
        if existing is None:
            existing = Verse(chapter_id=chapter.id, **v)
            db.add(existing)
            db.flush()
            counts["verses"] += 1
        verses_by_ref[v["verse_ref"]] = existing

    for q_number, domain, text, ref, verse_ref in _QUESTIONS:
        existing_q = db.execute(select(Question).where(Question.q_number == q_number)).scalar_one_or_none()
        if existing_q is None:
            existing_q = Question(
                q_number=q_number, domain=domain, question_text=text, gita_reference=ref
            )
            db.add(existing_q)
            db.flush()
            counts["questions"] += 1
        # Link question -> cites_verse -> verse (idempotent-ish; only on fresh question).
        verse = verses_by_ref.get(verse_ref)
        if verse is not None and counts["questions"]:
            rel_exists = db.execute(
                select(Relationship).where(
                    Relationship.from_type == "question",
                    Relationship.from_id == existing_q.id,
                    Relationship.to_type == "verse",
                    Relationship.to_id == verse.id,
                    Relationship.relation == "cites_verse",
                )
            ).scalar_one_or_none()
            if rel_exists is None:
                db.add(
                    Relationship(
                        from_type="question", from_id=existing_q.id,
                        to_type="verse", to_id=verse.id, relation="cites_verse",
                    )
                )
                counts["relationships"] += 1

    db.commit()
    return counts
