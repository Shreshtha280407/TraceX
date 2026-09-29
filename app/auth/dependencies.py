"""Development identity header plus server-side membership enforcement."""

# ruff: noqa: B008  # FastAPI declares dependencies through parameter defaults.
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.firebase import verify_firebase_token
from app.db import get_session
from app.models import Case, CaseMembership, User


def current_user(
    authorization: str | None = Header(default=None),
    x_tracex_actor: str | None = Header(default=None, alias="X-TraceX-Actor"),
    session: Session = Depends(get_session),
) -> User:
    """Real signed-in identity via `Authorization: Bearer <Firebase ID token>`, falling
    back to the dev-only `X-TraceX-Actor` header for local development and tests."""
    if authorization and authorization.startswith("Bearer "):
        claims = verify_firebase_token(authorization.removeprefix("Bearer "))
        if claims is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        # The verified email, not the opaque Firebase uid: `add_case_member` looks up
        # (and can pre-create) a teammate's User row by `external_subject` *before*
        # they have ever signed in, so it must be a value a case lead can type in --
        # an email address, not a random UID nobody can guess. Both enabled sign-in
        # methods (email/password, Google) always carry a verified email.
        subject = claims.get("email")
        if not subject:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has no verified email")
        user = session.scalar(select(User).where(User.external_subject == subject))
        if user is None:
            # First time this Firebase identity has reached the backend -- provision
            # the local User row lazily, exactly like the dev-header path below.
            user = User(external_subject=subject)
            session.add(user)
            session.commit()
            session.refresh(user)
        return user
    if not x_tracex_actor or len(x_tracex_actor) > 128:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-TraceX-Actor is required")
    user = session.scalar(select(User).where(User.external_subject == x_tracex_actor))
    if user is None:
        user = User(external_subject=x_tracex_actor)
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def require_case_member(case_id: str, user: User, session: Session) -> Case:
    case = session.get(Case, case_id)
    membership = session.scalar(
        select(CaseMembership).where(CaseMembership.case_id == case_id, CaseMembership.user_id == user.id)
    )
    if case is None or membership is None:
        # Deliberately do not reveal whether the case exists to a non-member.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return case
