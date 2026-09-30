"""Development identity header plus server-side membership enforcement."""

# ruff: noqa: B008  # FastAPI declares dependencies through parameter defaults.
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import verify_session_token
from app.config import settings
from app.db import get_session
from app.models import Case, CaseMembership, User


def current_user(
    authorization: str | None = Header(default=None),
    x_tracex_actor: str | None = Header(default=None, alias="X-TraceX-Actor"),
    session: Session = Depends(get_session),
) -> User:
    """Real signed-in identity via `Authorization: Bearer <token>`, falling back to the
    dev-only `X-TraceX-Actor` header only when TRACEX_ALLOW_DEV_ACTOR_HEADER=1 -- without
    that gate, anyone could name any actor via a header and skip login entirely."""
    if authorization and authorization.startswith("Bearer "):
        subject = verify_session_token(authorization.removeprefix("Bearer "))
        if subject is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        user = session.scalar(select(User).where(User.external_subject == subject))
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return user
    if not settings.allow_dev_actor_header:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization Bearer token is required")
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
