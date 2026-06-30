"""Tiny management CLI.

    python -m app.cli seed                         # load starter questions/verses
    python -m app.cli create-admin EMAIL PASSWORD  # create/promote an admin
    python -m app.cli copy-data SOURCE_URL TARGET_URL [--truncate] [--content]
        # clone data from one Postgres into another (e.g. staging -> prod)

Admin can also be granted automatically via the ADMIN_EMAILS env var.
"""

from __future__ import annotations

import sys

from app.db import SessionLocal

# Knowledge/content tables — the promotable set (excludes user accounts,
# chat history, analytics, and the settings row that holds API keys).
CONTENT_TABLES = {
    "chapters", "verses", "topics", "concepts", "questions", "relationships",
    "kb_sources", "kb_answers", "kb_chunks", "public_kb_articles",
    "ai_config", "llm_baselines",
}


def _need_db():
    if SessionLocal is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        raise SystemExit(1)


def cmd_seed() -> None:
    _need_db()
    from app.services.seed import seed_starter

    with SessionLocal() as db:
        counts = seed_starter(db)
    print("Seeded:", counts)


def cmd_create_admin(email: str, password: str) -> None:
    _need_db()
    from app.auth import service

    with SessionLocal() as db:
        user = service.get_by_email(db, email)
        if user is None:
            user = service.create_email_user(db, email, password, name=None)
            user.role = "admin"
            db.commit()
            print(f"Created admin {email} (id={user.id}).")
        else:
            from app.auth.security import hash_password

            user.role = "admin"
            user.password_hash = hash_password(password)
            db.commit()
            print(f"Promoted {email} to admin and reset password.")


def cmd_copy_data(source_url: str, target_url: str, truncate: bool, content_only: bool) -> None:
    """Copy table data SOURCE_URL -> TARGET_URL in FK-dependency order.

    Schemas must already match (both run the same migration). Embeddings and
    jsonb copy as-is. By default copies ALL tables; --content limits to the
    knowledge tables. --truncate clears the target first.
    """
    from sqlalchemy import create_engine, insert, select, text

    from app.db import _normalize_url
    from app.models import Base

    src = create_engine(_normalize_url(source_url), future=True)
    dst = create_engine(_normalize_url(target_url), future=True)

    ordered = list(Base.metadata.sorted_tables)  # FK-safe (parents first)
    if content_only:
        ordered = [t for t in ordered if t.name in CONTENT_TABLES]

    if truncate:
        with dst.begin() as conn:
            for table in reversed(ordered):
                conn.execute(text(f'TRUNCATE TABLE {table.name} RESTART IDENTITY CASCADE'))
        print(f"Truncated {len(ordered)} target tables.")

    total = 0
    with src.connect() as s_conn, dst.begin() as d_conn:
        for table in ordered:
            rows = [dict(r._mapping) for r in s_conn.execute(select(table))]
            if rows:
                for i in range(0, len(rows), 500):  # batch
                    d_conn.execute(insert(table), rows[i : i + 500])
            # Keep identity sequences ahead of the copied ids.
            if "id" in table.c:
                d_conn.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                        f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM {table.name}), 1))"
                    )
                )
            print(f"  {table.name}: {len(rows)} rows")
            total += len(rows)
    print(f"Done. Copied {total} rows across {len(ordered)} tables.")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, *rest = argv
    if cmd == "seed":
        cmd_seed()
    elif cmd == "create-admin" and len(rest) == 2:
        cmd_create_admin(rest[0], rest[1])
    elif cmd == "copy-data" and len(rest) >= 2:
        flags = {a for a in rest[2:]}
        cmd_copy_data(rest[0], rest[1], truncate="--truncate" in flags, content_only="--content" in flags)
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
