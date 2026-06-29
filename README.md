# AI Gita Mentor

The digital extension of **Ganga Narayan Das (GND)** — Gita wisdom applied to the
modern nervous system via the Neuro-Acoustic Protocol. Hosted at
`ai.applygitawisdom.com`.

Built as a **modular monolith**: one FastAPI app, one Postgres (with pgvector),
clean internal module boundaries — *not* microservices. See
`AI_Gita_Mentor_BuildSpec_v2.md` for the full design brief.

## The four systems (modules in one app)

1. **Marketing** — landing, pricing, CTA → *Try Free AI Mentor*.
2. **Public Knowledge Graph (System A)** — crawlable, ungated `/learn/*` pages.
3. **AI Mentor (System B)** — the gated, tiered chat product (Claude + hybrid RAG).
4. **Knowledge Studio (Admin)** — ingest → review → chunk → embed → publish.

## Stack

- Python 3.12 · FastAPI · Uvicorn
- Postgres + **pgvector** (HNSW, cosine) — embeddings are OpenAI
  `text-embedding-3-small` (**1536 dims**)
- Claude (chat, prompt-cached system prompt) · Razorpay (billing) · Google Drive (audio archive)
- Schema owned by **Alembic** migrations (`migrations/`)

## Project layout

```
app/
  config.py            # env-driven settings (Section 15)
  db.py                # engine, session, connectivity helpers
  main.py              # FastAPI app + router mounts
  models/              # the normalized knowledge graph (Section 4)
  routers/             # health + public (robots/sitemap/landing)
  services/retrieval.py# canonical tier-gated vector search
migrations/            # Alembic (0001 enables pgvector + builds the schema)
templates/             # branded server-rendered pages
```

## Local development

```bash
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
cp .env.example .env                                # then fill DATABASE_URL etc.
alembic upgrade head                                # create schema + pgvector + indexes
uvicorn app.main:app --reload
```

Open http://localhost:8000 (landing) · `/healthz` · `/readyz` · `/api/docs`.

> Use Python **3.12** locally to match the deploy target — some pinned wheels
> may lag on the newest interpreters.

## Deployment (Railway)

- Railpack detects Python via `requirements.txt`; `.python-version` pins 3.12.
- Start command (`Procfile` / `railway.json`) runs
  `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`,
  so migrations apply on every deploy before the server starts.
- Health check: `/healthz` (never touches the DB).
- Set the env vars from `.env.example` in the Railway service.
- If a migration reports `extension "vector" is not available`, switch the
  Postgres service image to `pgvector/pgvector:pg16`.

## The paywall is in the database

`kb_chunks.min_tier` (smallint: 0=seeker, 1=abhyasi, 2=sadhaka) gates retrieval.
A free user's query carries tier level 0, so `WHERE min_tier <= 0` can only ever
return Seeker chunks — recorded paid depth is unreachable, not just un-prompted.

## Build status

Phase 1 (scaffold + data model + deploy contract) is in place. Subsequent
phases — recorder + LLM baseline panel, ingestion, hybrid retrieval + chat,
billing, public KB, admin — follow the v1 critical path in Section 16.
