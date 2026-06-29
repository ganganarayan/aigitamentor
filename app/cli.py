"""Tiny management CLI.

    python -m app.cli seed                         # load starter questions/verses
    python -m app.cli create-admin EMAIL PASSWORD  # create/promote an admin

Admin can also be granted automatically via the ADMIN_EMAILS env var.
"""

from __future__ import annotations

import sys

from app.db import SessionLocal


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


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, *rest = argv
    if cmd == "seed":
        cmd_seed()
    elif cmd == "create-admin" and len(rest) == 2:
        cmd_create_admin(rest[0], rest[1])
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
