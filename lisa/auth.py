import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lisa.db import get_db
from lisa.models import Account, Invite, LoginSession
from lisa.security import hash_password, hash_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["account"])
DB = Annotated[Session, Depends(get_db)]
COOKIE = "lisa_session"
# Equal password work for unknown users; never a usable account credential.
DUMMY_PASSWORD = hash_password(secrets.token_urlsafe(32))


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower() if isinstance(value, str) else value


class Registration(Credentials):
    invite_code: str = Field(min_length=1, max_length=128)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    username: str


def issue_invites(db: Session, count: int) -> list[str]:
    if not 1 <= count <= 20:
        raise ValueError("邀请数量须为 1–20。")
    # Issuance is rare; one table lock makes the initial 20-place limit atomic.
    db.execute(text("LOCK TABLE invites IN EXCLUSIVE MODE"))
    if db.scalar(select(func.count()).select_from(Invite)) + count > 20:
        raise ValueError("首批最多发放 20 个邀请名额（含已使用）。")
    codes = [secrets.token_urlsafe(18) for _ in range(count)]
    db.add_all(Invite(code_hash=hash_token(code)) for code in codes)
    db.flush()
    return codes


def current_user(request: Request, db: DB) -> Account:
    token = request.cookies.get(COOKIE)
    if token:
        user = db.scalar(
            select(Account)
            .join(LoginSession, LoginSession.user_id == Account.id)
            .where(
                LoginSession.token_hash == hash_token(token),
                LoginSession.expires_at > datetime.now(UTC),
                Account.active.is_(True),
            )
        )
        if user:
            return user
    raise HTTPException(401, "请先登录，或重新登录已过期的会话。")


def start_session(db: Session, user: Account, request: Request, response: Response) -> None:
    old_token = request.cookies.get(COOKIE)
    if old_token:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == hash_token(old_token)))
    token = secrets.token_urlsafe(32)
    settings = request.app.state.settings
    seconds = settings.session_days * 86400
    db.add(
        LoginSession(
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        )
    )
    response.set_cookie(
        COOKIE,
        token,
        max_age=seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/register", response_model=UserView, status_code=201)
def register(body: Registration, request: Request, response: Response, db: DB):
    password_hash = hash_password(body.password)
    invite = db.scalar(
        select(Invite)
        .where(Invite.code_hash == hash_token(body.invite_code.strip()))
        .with_for_update()
    )
    if invite is None or invite.consumed or invite.used_by is not None:
        raise HTTPException(400, "邀请码无效或已使用。")
    user = Account(username=body.username, password_hash=password_hash)
    db.add(user)
    try:
        db.flush()
        invite.used_by = user.id
        invite.consumed = True
        start_session(db, user, request, response)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "这个用户名已被使用，请换一个。") from None
    return user


@router.post("/login", response_model=UserView)
def login(body: Credentials, request: Request, response: Response, db: DB):
    user = db.scalar(select(Account).where(Account.username == body.username).with_for_update())
    valid = verify_password(body.password, user.password_hash if user else DUMMY_PASSWORD)
    if not valid or user is None or not user.active:
        raise HTTPException(401, "用户名或密码不正确。")
    start_session(db, user, request, response)
    db.commit()
    return user


@router.get("/me", response_model=UserView)
def me(user: Annotated[Account, Depends(current_user)]):
    return user


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: DB):
    token = request.cookies.get(COOKIE)
    if token:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == hash_token(token)))
        db.commit()
    response.delete_cookie(
        COOKIE,
        httponly=True,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
