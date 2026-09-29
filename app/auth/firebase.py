"""Firebase ID token verification for the real signed-in identity path.

This is the one code path in TraceX that is not offline-capable: verifying a
Firebase ID token needs the Firebase Admin SDK, which fetches Google's public
signing certificates over HTTPS on first use (cached per the SDK's own TTL
afterward). Ingestion, the graph, findings and evidence export stay fully
offline; only sign-in and token verification need connectivity, and that
network call happens here, nowhere else.

`app.auth.dependencies.current_user` calls `verify_firebase_token` directly
so it stays trivially mockable in tests without a real Firebase project.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import settings


@lru_cache(maxsize=1)
def _firebase_app():
    import firebase_admin
    from firebase_admin import credentials

    if settings.firebase_credentials_path is None:
        raise RuntimeError(
            "TRACEX_FIREBASE_CREDENTIALS_PATH is not set (or the file is missing); "
            "Firebase-authenticated requests cannot be verified. The X-TraceX-Actor "
            "header remains available for local development."
        )
    cred = credentials.Certificate(str(settings.firebase_credentials_path))
    return firebase_admin.initialize_app(cred)


def verify_firebase_token(id_token: str) -> dict | None:
    """Decoded claims (at least `uid`) for a valid, unexpired Firebase ID token, else `None`."""
    from firebase_admin import auth as firebase_auth

    try:
        return firebase_auth.verify_id_token(id_token, app=_firebase_app())
    except Exception:  # noqa: BLE001 — any invalid/expired/malformed token is simply unauthenticated
        return None
