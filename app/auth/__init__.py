"""Authentication & authorization module.

Google OAuth primary + email/password fallback, stateless JWT sessions stored
in an HTTP-only cookie. Admin role gates /admin/* (Section 13).
"""
