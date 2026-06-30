"""Audio transcription via OpenAI gpt-4o-mini-transcribe (Section 6).

A Sanskrit-aware prompt hint nudges the model on shloka terminology. Raises
RuntimeError with a clear message if the key is missing — the caller turns that
into recording status='failed' with error_text, never a crash.
"""

from __future__ import annotations

_SANSKRIT_HINT = (
    "This audio discusses the Bhagavad Gita and the Neuro-Acoustic Protocol. "
    "Expect Sanskrit terms such as dharma, karma, sthira, rajas, sattva, "
    "chitta vritti, abhyasa, sadhaka, and verse references like 2.47."
)


def transcribe_file(path: str, api_key: str | None, model: str) -> str:
    if not api_key:
        raise RuntimeError("OpenAI API key not configured")
    import openai

    client = openai.OpenAI(api_key=api_key)
    with open(path, "rb") as fh:
        result = client.audio.transcriptions.create(
            model=model,
            file=fh,
            prompt=_SANSKRIT_HINT,
        )
    return getattr(result, "text", "") or ""
