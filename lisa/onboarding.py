import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lisa.auth import DB, current_user
from lisa.models import (
    Account,
    GuideNotice,
    LoginSession,
    Persona,
    PersonaVersion,
    QQBinding,
    ServiceCode,
    ServiceEvent,
)
from lisa.security import hash_password, hash_token

router = APIRouter(tags=["initialization"])
User = Annotated[Account, Depends(current_user)]


class CodeInput(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class BindingConfirmation(CodeInput):
    qq_id: str


class PersonaInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=12000)

    @field_validator("name", "prompt", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class PersonaView(PersonaInput):
    model_config = ConfigDict(from_attributes=True)
    version: int


class SetupView(BaseModel):
    qq_id: str | None
    initialized: bool
    persona: PersonaView | None
    presets: list = []  # Formal presets await D07; never substitute test content.


class PasswordReset(BaseModel):
    reset_token: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=10, max_length=128)


def setup_state(db: Session, user: Account) -> SetupView:
    binding = db.get(QQBinding, user.id)
    persona = db.scalar(select(Persona).where(Persona.user_id == user.id))
    return SetupView(
        qq_id=binding.qq_id if binding else None,
        initialized=bool(binding and persona),
        persona=PersonaView.model_validate(persona) if persona else None,
    )


def issue_code(db: Session, purpose: str, qq_id: str, user_id=None) -> str:
    prefix = {"binding": "B", "recovery": "R", "reset": "T"}[purpose]
    code = f"{prefix}-{secrets.token_urlsafe(18)}"
    db.execute(
        delete(ServiceCode).where(ServiceCode.qq_id == qq_id, ServiceCode.purpose == purpose)
    )
    db.add(
        ServiceCode(
            code_hash=hash_token(code),
            purpose=purpose,
            qq_id=qq_id,
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    db.flush()
    return code


def valid_code(db: Session, token: str, purpose: str, *, lock: bool = True) -> ServiceCode:
    query = select(ServiceCode).where(
        ServiceCode.code_hash == hash_token(token.strip()), ServiceCode.purpose == purpose
    )
    if lock:
        query = query.with_for_update()
    code = db.scalar(query)
    if code is None or code.used_at is not None or code.expires_at <= datetime.now(UTC):
        raise HTTPException(400, "服务码无效、已过期或已使用，请重新领取对应的服务码。")
    return code


@router.get("/api/setup", response_model=SetupView)
def get_setup(user: User, db: DB):
    return setup_state(db, user)


@router.get("/api/setup/presets")
def presets(user: User):
    return []


@router.post("/api/setup/binding/preview")
def preview_binding(body: CodeInput, user: User, db: DB):
    if db.get(QQBinding, user.id):
        raise HTTPException(409, "当前账号已绑定 QQ，更换绑定请联系管理员核实。")
    code = valid_code(db, body.code, "binding", lock=False)
    if db.scalar(select(QQBinding).where(QQBinding.qq_id == code.qq_id)):
        raise HTTPException(409, "此 QQ 已绑定账号，请登录或找回原账号。")
    return {"qq_id": code.qq_id, "expires_at": code.expires_at}


@router.post("/api/setup/binding/confirm", response_model=SetupView)
def confirm_binding(body: BindingConfirmation, user: User, db: DB):
    code = valid_code(db, body.code, "binding")
    db.execute(select(Account).where(Account.id == user.id).with_for_update())
    if body.qq_id != code.qq_id:
        raise HTTPException(400, "核对的 QQ 与服务码不一致，请重新核对。")
    if db.get(QQBinding, user.id):
        raise HTTPException(409, "当前账号已绑定 QQ，更换绑定请联系管理员核实。")
    code.used_at = datetime.now(UTC)
    db.add(QQBinding(user_id=user.id, qq_id=code.qq_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "此 QQ 已绑定账号，请登录或找回原账号。") from None
    return setup_state(db, user)


@router.post("/api/setup/persona", response_model=SetupView)
def save_persona(body: PersonaInput, user: User, db: DB):
    db.execute(select(Account).where(Account.id == user.id).with_for_update())
    if not db.get(QQBinding, user.id):
        raise HTTPException(403, "请先绑定本人 QQ，再保存 LISA 性格。")
    persona = db.scalar(select(Persona).where(Persona.user_id == user.id))
    if persona and persona.name == body.name and persona.prompt == body.prompt:
        return setup_state(db, user)
    from lisa.style_profiles import clear_personality_application

    clear_personality_application(db, user.id)
    if persona:
        persona.name, persona.prompt = body.name, body.prompt
        persona.version += 1
    else:
        persona = Persona(user_id=user.id, name=body.name, prompt=body.prompt, version=1)
        db.add(persona)
    db.flush()
    db.add(
        PersonaVersion(
            persona_id=persona.id, version=persona.version, name=persona.name, prompt=persona.prompt
        )
    )
    db.commit()
    return setup_state(db, user)


@router.post("/api/auth/recovery/verify")
def verify_recovery(body: CodeInput, db: DB):
    code = valid_code(db, body.code, "recovery")
    user = db.get(Account, code.user_id)
    binding = db.get(QQBinding, code.user_id)
    if user is None or not user.active or binding is None or binding.qq_id != code.qq_id:
        raise HTTPException(400, "账号当前无法找回，请联系管理员核实。")
    code.used_at = datetime.now(UTC)
    ticket = issue_code(db, "reset", code.qq_id, user.id)
    db.commit()
    return {"username": user.username, "reset_token": ticket}


@router.post("/api/auth/recovery/reset", status_code=204)
def reset_password(body: PasswordReset, db: DB):
    password_hash = hash_password(body.password)
    code = valid_code(db, body.reset_token, "reset")
    user = db.scalar(select(Account).where(Account.id == code.user_id).with_for_update())
    if user is None or not user.active:
        raise HTTPException(400, "账号当前无法找回，请联系管理员核实。")
    user.password_hash = password_hash
    code.used_at = datetime.now(UTC)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    db.commit()


def handle_service_message(
    db: Session,
    *,
    bot_id: str,
    message_id: str,
    qq_id: str,
    sent_at: datetime,
    text: str,
    web_url: str,
) -> tuple[str, str | None]:
    """Handle one gateway event. Caller commits before sending; never invokes a model."""
    event_id = f"{bot_id}:{message_id}"
    inserted = db.scalar(
        insert(ServiceEvent)
        .values(
            id=event_id,
            bot_id=bot_id,
            message_id=message_id,
            qq_id=qq_id,
            sent_at=sent_at,
            status="received",
        )
        .on_conflict_do_nothing()
        .returning(ServiceEvent.id)
    )
    if inserted is None:
        return event_id, None
    binding = db.scalar(select(QQBinding).where(QQBinding.qq_id == qq_id))
    user = db.get(Account, binding.user_id) if binding else None
    command = text.strip()
    if user and not user.active:
        reply = "账号当前不可用，请联系邀请你的管理员核实。"
    elif command == "绑定":
        if binding:
            reply = f"此 QQ 已绑定账号，请登录或发送“找回”找回原账号。网页：{web_url}"
        else:
            code = issue_code(db, "binding", qq_id)
            reply = (
                f"绑定码：{code}\n15 分钟内有效，仅供绑定本人网页账号，勿转发。"
                f"重新领取后旧码失效。\n请登录 {web_url} 提交并核对 QQ 号。"
            )
    elif command == "找回":
        if not binding:
            reply = f"此 QQ 尚未绑定账号。请先到网页注册并绑定 QQ：{web_url}"
        else:
            code = issue_code(db, "recovery", qq_id, binding.user_id)
            reply = (
                f"找回码：{code}\n15 分钟内有效，仅供找回本人账号，勿转发。"
                f"重新领取后旧码失效。\n请到 {web_url} 的账号找回入口验证。"
            )
    else:
        persona = db.scalar(select(Persona).where(Persona.user_id == user.id)) if user else None
        stage = "binding" if not binding else "persona" if not persona else "ready"
        notice = db.get(GuideNotice, qq_id)
        now = datetime.now(UTC)
        if stage == "ready" or (
            notice and notice.stage == stage and now - notice.sent_at < timedelta(minutes=5)
        ):
            reply = None
        else:
            reply = (
                f"请先到网页注册并绑定 QQ：{web_url}"
                if stage == "binding"
                else f"请到网页完成性格选择：{web_url}"
            )
            if notice:
                notice.stage, notice.sent_at = stage, now
            else:
                db.add(GuideNotice(qq_id=qq_id, stage=stage, sent_at=now))
    if reply is None:
        db.get(ServiceEvent, event_id).status = "ignored"
    return event_id, reply


def record_delivery(db: Session, event_key: str, status: str) -> None:
    event = db.get(ServiceEvent, event_key)
    if event is not None:
        event.status = status
