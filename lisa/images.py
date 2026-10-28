"""Private work pictures and asynchronous chat drawings; no consumption quotas."""

import asyncio
import base64
import io
import json
import re
import warnings
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from openai import APIStatusError, AsyncOpenAI, DefaultAsyncHttpxClient, OpenAIError
from PIL import Image, UnidentifiedImageError
from procrastinate.exceptions import JobRetry
from procrastinate.retry import RetryDecision
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.orm import Session

from lisa.auth import DB
from lisa.model_calls import (
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    _finish,
    _reserve,
    generate_structured,
)
from lisa.models import (
    Chapter,
    Character,
    ImagePreference,
    ModelCall,
    OutboundMessage,
    PrivateImage,
    Project,
    QQBinding,
    Task,
)
from lisa.works import (
    User,
    document_text,
    lock_account,
    require_chapter,
    require_initialized,
    require_project,
)

router = APIRouter(tags=["images"])
MAX_IMAGE_BYTES = 12 * 1024 * 1024
STYLES = {
    "watercolor": (
        "文学叙事水彩插画，细线勾勒，透明水彩分层晕染，保留纸纹和边缘渗化；"
        "有限自然色、低饱和，主体清晰、背景适度简化。保持手绘水彩质感，"
        "避免摄影写实、三维渲染、厚重油画。昼夜、天气和具体颜色遵循场景。"
    ),
    "woodcut": (
        "黑白木刻版画式文学插图，仅用黑与纸白，以明确刀刻线条、疏密排线和留白塑形，"
        "强明暗对比，清晰轮廓和纸上印刷质感；避免彩色、灰阶喷绘、水彩和摄影质感。"
    ),
    "animation": (
        "二维动画原画式插图，统一粗细的清晰线稿，平整色块与少量赛璐璐阴影，"
        "有限协调色盘、自然表情、简练有层次的背景；避免照片、三维材质、复杂体积光、"
        "水彩或油画笔触。昼夜、天气和具体颜色遵循场景。"
    ),
}
IMAGE_INSTRUCTIONS = (
    "\nimage_action默认null。图片只在有必要时偶尔使用，普通交流以文字为主，"
    "不要连续多轮配图，也不用图片填补回复。operation=generate生成一张图片；"
    "小说插图/封面必须用户本轮明确委托且目标清楚，requested=true且evidence逐字引用委托；"
    "用提供的project_id和chapter_id，不猜不存在的章节。目标不清则image_action=null并先问清。"
    "target=cover书封、illustration章节插图、chat本轮私聊小图；聊天小图不关联作品。"
    "goal只写必要绘图内容（主体、已知外观、场景、动作、构图），不要复制原始对话或无关私人资料，"
    "不可当作作品/现实事实。LISA出镜必须depicts_lisa=true且有本账号指定人设参考，否则先请用户提供。"
    "requested=false只可用于当前话题特别适合的一张聊天小图，不另起主动联系。"
    "style仅选watercolor水彩叙事/woodcut黑白版画/animation平涂动画；null沿用作品画风。"
    "用户仅选择/调整画风、没有要求本次作画时才operation=style。"
    "同一句要求画图并指定画风时必须operation=generate，style只附带本次画风选择，不能只保存画风。"
    "style_notes仅保存必要补充，evidence引用选择。"
    "用户明确指定本轮所发图片作为LISA人设或作品画风参考时operation=reference，"
    "target=chat表示本账号LISA人设，cover/illustration表示该作品画风；不要把普通上传自动设为参考。"
    "用户明确取消正在绘图时operation=cancel，target和作品/章节指定要取消的范围。"
    "生成是异步委托，文字可告知稍后送达，不声称图片已经完成。"
)


class ImageAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["generate", "style", "reference", "cancel"] = "generate"
    target: Literal["cover", "illustration", "chat"]
    project_id: UUID | None = None
    chapter_id: UUID | None = None
    goal: str = Field(default="", max_length=3000)
    style: Literal["watercolor", "woodcut", "animation"] | None = None
    style_notes: str = Field(default="", max_length=1000)
    depicts_lisa: bool = False
    requested: bool = False
    evidence: str = Field(default="", max_length=1000)


class ImagePreparation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_action: ImageAction | None
    clarification: str = Field(default="", max_length=500)


def image_enabled(settings):
    return bool(
        settings.image_model
        and settings.image_base_url
        and settings.image_api_key.get_secret_value()
    )


def image_intent(texts):
    # Only routes drawing-related turns to an exempt description call. It never authorizes a task.
    return bool(re.search(r"画|绘|配图|插图|封面|书封|图片|人设|发.*图", "\n".join(texts)))


def image_context(db, user_id):
    preference = db.get(ImagePreference, user_id)
    projects = list(db.scalars(select(Project).where(Project.user_id == user_id)))
    rows = []
    for project in projects:
        chapters = db.execute(
            select(Chapter.id, Chapter.title)
            .where(Chapter.project_id == project.id)
            .order_by(Chapter.position)
            .limit(100)
        ).all()
        rows.append(
            {
                "project_id": str(project.id),
                "title": project.title,
                "current": project.is_current,
                "style": project.image_style,
                "chapters": [{"id": str(c.id), "title": c.title} for c in chapters],
            }
        )
    recent = db.scalar(
        select(Task)
        .where(Task.user_id == user_id, Task.kind == "image")
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    return (
        IMAGE_INSTRUCTIONS
        + "\n图片可用资料："
        + json.dumps(
            {
                "lisa_reference_available": bool(preference and preference.lisa_reference_id),
                "projects": rows,
                "recent_drawing": {
                    "status": recent.status,
                    "target": recent.request.get("target"),
                    "time": recent.created_at.isoformat(),
                }
                if recent
                else None,
            },
            ensure_ascii=False,
        )
    )


async def prepare_image_action(settings, engine, task_id, user_id, texts):
    with Session(engine) as db:
        context = image_context(db, user_id)
    return await generate_structured(
        settings,
        engine,
        task_id,
        user_id,
        "仅准备图片委托，不做普通聊天、写作、记忆或事实更新。没有明确图片委托/选择/取消时"
        "image_action=null、clarification为空。目标/关键外观不明时只用clarification简短澄清。"
        + context,
        [{"role": "user", "content": "\n".join(texts)}],
        schema=ImagePreparation,
        image_description=True,
        output_tokens=1800,
    )


def normalize_image(data: bytes) -> tuple[bytes, str]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片须小于 12 MB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("请使用 PNG、JPEG 或 WebP 图片。")
                width, height = image.size
                if (
                    min(width, height) < 1
                    or max(width, height) > 8192
                    or width * height > 32_000_000
                ):
                    raise ValueError("图片尺寸过大，请缩小后上传。")
                image.load()
                result = io.BytesIO()
                decoded = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                decoded.info.clear()
                decoded.save(result, "PNG")
                body = result.getvalue()
                if len(body) > MAX_IMAGE_BYTES:
                    raise ValueError("图片解码后过大，请缩小后上传。")
                return body, "image/png"
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("图片损坏或尺寸过大，请换一张。") from exc


def store_image(db, user_id, data, *, origin, temporary=False):
    data, mime = normalize_image(data)
    item = PrivateImage(
        user_id=user_id,
        data=data,
        mime=mime,
        origin=origin,
        expires_at=datetime.now(UTC) + timedelta(hours=24) if temporary else None,
    )
    db.add(item)
    db.flush()
    return item


def owned_image(db, user_id, image_id):
    item = db.get(PrivateImage, image_id) if image_id else None
    if (
        item
        and item.user_id == user_id
        and (item.expires_at is None or item.expires_at > datetime.now(UTC))
    ):
        return item
    return None


@router.get("/api/private-images/{image_id}")
def read_image(image_id: UUID, db: DB, user: User):
    item = owned_image(db, user.id, image_id)
    if item is None:
        raise HTTPException(404, "图片不存在。")
    return Response(
        item.data,
        media_type=item.mime,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


class UploadImage(BaseModel):
    content_base64: str = Field(min_length=1, max_length=16_777_216)


def upload_into_slot(db, user, slot, field, body):
    try:
        data = base64.b64decode(body.content_base64, validate=True)
        item = store_image(db, user.id, data, origin="upload")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    setattr(slot, field, item.id)
    slot.image_revision += 1
    db.commit()
    return {"image_id": str(item.id)}


@router.put("/api/projects/{project_id}/cover")
def upload_cover(project_id: UUID, body: UploadImage, db: DB, user: User):
    user = lock_account(db, user.id)
    require_initialized(db, user)
    return upload_into_slot(db, user, require_project(db, user, project_id), "cover_image_id", body)


@router.get("/api/projects/{project_id}/cover")
def cover_info(project_id: UUID, db: DB, user: User):
    item = require_project(db, user, project_id)
    return {"image_id": str(item.cover_image_id) if item.cover_image_id else None}


@router.put("/api/chapters/{chapter_id}/illustration")
def upload_illustration(chapter_id: UUID, body: UploadImage, db: DB, user: User):
    user = lock_account(db, user.id)
    require_initialized(db, user)
    return upload_into_slot(
        db, user, require_chapter(db, user, chapter_id), "illustration_image_id", body
    )


@router.get("/api/chapters/{chapter_id}/illustration")
def illustration_info(chapter_id: UUID, db: DB, user: User):
    item = require_chapter(db, user, chapter_id)
    return {"image_id": str(item.illustration_image_id) if item.illustration_image_id else None}


def apply_image_action(db, settings, user, source_task, action, current_texts, reference_data=None):
    """Account lock is held by the chat adopter. Persist only the necessary drawing brief."""
    if action is None:
        return ""
    explicit = (
        action.requested
        and action.evidence.strip()
        and any(action.evidence in text for text in current_texts)
    )
    if not explicit and (action.operation != "generate" or action.target != "chat"):
        return "图片目标还不明确，请说明要画哪部作品的封面或哪个章节。"
    project, chapter = None, None
    if action.target != "chat":
        project = db.get(Project, action.project_id) if action.project_id else None
        if project is None or project.user_id != user.id:
            return "请先说清要为哪部作品配图。"
        if action.target == "illustration":
            chapter = db.get(Chapter, action.chapter_id) if action.chapter_id else None
            if chapter is None or chapter.project_id != project.id:
                return "请先说清要配图的章节。"
    if action.operation == "cancel":
        tasks = db.scalars(
            select(Task).where(
                Task.user_id == user.id,
                Task.kind == "image",
                Task.status.in_(["pending", "running"]),
            )
        ).all()
        for task in tasks:
            if (
                task.request.get("target") == action.target
                and (action.target == "chat" or task.project_id == project.id)
                and (not chapter or task.chapter_id == chapter.id)
            ):
                task.status, task.error = "cancelled", "user_cancelled"
                task.finished_at = datetime.now(UTC)
                task.input_snapshot = None
        return "已取消这个范围内尚未保存的绘图。"
    if action.operation == "reference":
        if reference_data is None:
            return "请把要指定的人设或画风参考图和说明一起发来。"
        item = store_image(db, user.id, reference_data, origin="reference")
        if project:
            project.image_reference_id = item.id
            project.image_revision += 1
        else:
            preference = db.get(ImagePreference, user.id)
            if preference is None:
                preference = ImagePreference(user_id=user.id)
                db.add(preference)
            preference.lisa_reference_id = item.id
        return "已保存这张指定参考图，之后绘图会沿用。"
    if action.operation == "style":
        if project is None or action.style is None:
            return "请说明作品和要采用的画风。"
        project.image_style, project.image_notes = action.style, action.style_notes
        project.image_revision += 1
        return "已保存这部作品的画风。"
    if not image_enabled(settings):
        return "图片服务尚未配置好，这次没有开始绘图。"
    if not action.goal.strip():
        return "请再说明这张图要表现的场景。"
    if db.scalar(
        select(Task.id)
        .where(
            Task.user_id == user.id, Task.kind == "image", Task.status.in_(["pending", "running"])
        )
        .limit(1)
    ):
        return "还有一张图正在画，这次没有重复发起。"
    if not explicit:
        # Avoid consecutive spontaneous drawings, without a daily quota or time cooldown.
        prior_chat = db.scalar(
            select(Task)
            .where(Task.user_id == user.id, Task.kind == "chat", Task.id != source_task.id)
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        if prior_chat and db.scalar(
            select(Task.id)
            .where(
                Task.user_id == user.id,
                Task.kind == "image",
                Task.request["source_task_id"].astext == str(prior_chat.id),
            )
            .limit(1)
        ):
            return ""
        if user.last_active_at is None or datetime.now(UTC) - user.last_active_at >= timedelta(
            hours=24
        ):
            return ""
    preference = db.get(ImagePreference, user.id)
    reference_id = (
        preference.lisa_reference_id
        if action.depicts_lisa and preference
        else (project.image_reference_id if project and not action.depicts_lisa else None)
    )
    reference = owned_image(db, user.id, reference_id)
    if action.depicts_lisa and reference is None:
        return "画我出镜的图之前，请先发一张图片并明确指定为我的人设参考。"
    if project and action.style and explicit:
        project.image_style = action.style
        project.image_notes = action.style_notes
        project.image_revision += 1
    slot = chapter or project
    style = project.image_style if project else (action.style or "animation")
    constraints = ""
    if project:
        characters = db.scalars(select(Character).where(Character.project_id == project.id)).all()
        constraints = (
            "\n作品约束："
            + project.constraints[:2000]
            + "\n人物资料："
            + json.dumps(
                [
                    {"name": c.name, "identity": c.definition.get("identity", "")}
                    for c in characters
                ],
                ensure_ascii=False,
            )[:6000]
        )
        constraints += "\n旧版人物资料：" + project.characters[:3000]
        constraints += "\n必要世界设定：" + project.worldbook[:3000]
        if chapter:
            constraints += "\n本章必要场景依据：" + document_text(chapter.content)[:6000]
    prompt = (
        (
            "沿用指定人设参考图的基础画风、媒介、线条与上色方式，不追求跨图严格一致。"
            if action.depicts_lisa
            else STYLES.get(style, STYLES["watercolor"])
        )
        + ("\n作品画风补充：" + project.image_notes if project else "")
        + "\n参考图只锚定约定画风或主要人物特征，不沿用无关背景/物件。"
        + (
            "\nLISA出镜，以参考图人物为基准，表情、动作和服装细节可自然变化。"
            if action.depicts_lisa
            else ""
        )
        + constraints
        + "\n本次画面："
        + action.goal
        + (
            "\n竖版书封构图，主体留出书名排版空间，画面不要绘制书名、作者或文字。"
            if action.target == "cover"
            else ""
        )
    )
    binding = db.get(QQBinding, user.id)
    task = Task(
        user_id=user.id,
        kind="image",
        source="qq",
        project_id=project.id if project else None,
        chapter_id=chapter.id if chapter else None,
        request={
            "target": action.target,
            "goal": action.goal,
            "style": style,
            "source_task_id": str(source_task.id),
            "requested": bool(explicit),
            "qq_id": binding.qq_id,
            "slot_revision": slot.image_revision if slot else None,
        },
        input_snapshot={"prompt": prompt, "reference_id": str(reference.id) if reference else None},
    )
    db.add(task)
    db.flush()
    from lisa.queue import enqueue_image

    task.queue_job_id = enqueue_image(db, task.id, user.id)
    return "已安排绘图，画好后会发来。"


async def reference_bytes(action, image_urls):
    if action is None or action.operation != "reference" or not image_urls:
        return None
    from lisa.vision import download_image

    result = await asyncio.to_thread(download_image, image_urls[-1])
    if result:
        try:
            return normalize_image(base64.b64decode(result.split(",", 1)[1]))[0]
        except ValueError:
            pass
    return None


async def execute_image_turn(
    settings, engine, task_id, user_id, texts, image_urls, expected_qq=None
):
    """Complete a clear drawing turn before any metered chat or retrieval call."""
    error, prepared, reference = None, None, None
    try:
        prepared = await prepare_image_action(settings, engine, task_id, user_id, texts)
        if prepared.image_action is None and not prepared.clarification:
            return False
        reference = await reference_bytes(prepared.image_action, image_urls)
    except ModelBusy:
        retry = False
        with Session(engine) as db, db.begin():
            user = lock_account(db, user_id)
            task = db.get(Task, task_id)
            if user and task and task.status == "running":
                task.status = "pending"
                retry = True
        if retry:
            raise JobRetry(RetryDecision(retry_in={"seconds": 3})) from None
        return True
    except ModelCallUnknown:
        error = "model_unknown"
    except (ModelCallFailed, ValueError):
        error = "image_description_failed"
    from lisa.chat import _outbound
    from lisa.models import ChatMessage

    with Session(engine) as db, db.begin():
        user = lock_account(db, user_id)
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None or task.status != "running":
            return True
        binding = db.get(QQBinding, user_id) if user else None
        task.status, task.error, task.finished_at = (
            "failed" if error else "completed",
            error,
            datetime.now(UTC),
        )
        if (
            user
            and user.active
            and binding
            and (expected_qq is None or binding.qq_id == expected_qq)
        ):
            if error:
                notice = "这次绘图委托未能确认，已停止自动重试，请稍后重新说明。"
            elif prepared.image_action:
                notice = apply_image_action(
                    db, settings, user, task, prepared.image_action, texts, reference
                )
            else:
                notice = prepared.clarification
            _outbound(db, task, binding.qq_id, notice or "这次没有发起绘图。")
        else:
            task.status, task.error = "cancelled", "account_unavailable"
        db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
        task.input_snapshot = None
    return True


async def generate_image(settings, engine, task_id, user_id, prompt, reference=None):
    call_id = _reserve(
        settings,
        engine,
        task_id,
        user_id,
        Decimal(0),
        budget_exempt=True,
        configuration={
            "purpose": "image_generation",
            "model": settings.image_model,
            "base_url": settings.image_base_url,
            "quality": "medium",
            "size": "1024x1024",
            "n": 1,
        },
    )
    try:
        async with AsyncOpenAI(
            api_key=settings.image_api_key.get_secret_value(),
            base_url=settings.image_base_url,
            max_retries=0,
            timeout=settings.image_timeout_seconds,
            http_client=DefaultAsyncHttpxClient(trust_env=False),
        ) as client:
            params = dict(
                model=settings.image_model,
                prompt=prompt,
                n=1,
                quality="medium",
                size="1024x1024",
                output_format="png",
            )
            if reference:
                result = await client.images.edit(
                    image=("reference.png", reference, "image/png"), **params
                )
            else:
                result = await client.images.generate(**params)
        encoded = result.data[0].b64_json
        if not encoded:
            raise ValueError("Provider did not return image bytes")
        data, _mime = normalize_image(base64.b64decode(encoded, validate=True))
        usage = result.usage.model_dump() if result.usage else None
    except APIStatusError as exc:
        unknown = exc.status_code >= 500 or exc.status_code in {408, 409}
        _finish(
            engine,
            call_id,
            state="unknown" if unknown else "failed",
            charged=Decimal(0),
            error="image_provider_unknown" if unknown else "image_rejected",
        )
        raise (ModelCallUnknown if unknown else ModelCallFailed)("Image provider failed") from None
    except OpenAIError:
        _finish(engine, call_id, state="unknown", error="image_transport_unknown")
        raise ModelCallUnknown("Image outcome unknown") from None
    except (ValueError, TypeError, AttributeError, IndexError):
        _finish(engine, call_id, state="failed", error="invalid_image_result")
        raise ModelCallFailed("Image result invalid") from None
    _finish(engine, call_id, state="succeeded", charged=Decimal(0), error="price_unavailable")
    with Session(engine) as db, db.begin():
        call = db.get(ModelCall, call_id)
        if call:
            call.configuration = {**call.configuration, "usage": usage, "cost_known": False}
    return data


async def execute_image(settings, engine, task_id, *, generate=None):
    task_id = UUID(str(task_id))
    with Session(engine) as db, db.begin():
        user_id = db.scalar(select(Task.user_id).where(Task.id == task_id))
        user = lock_account(db, user_id) if user_id else None
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None or task.status != "pending":
            return
        binding = db.get(QQBinding, user_id) if user else None
        if not user or not user.active or not binding or binding.qq_id != task.request.get("qq_id"):
            task.status, task.error = "cancelled", "account_unavailable"
            task.finished_at, task.input_snapshot = datetime.now(UTC), None
            return
        target = task.request["target"]
        slot = (
            db.get(Chapter, task.chapter_id)
            if target == "illustration"
            else (db.get(Project, task.project_id) if target == "cover" else None)
        )
        if target != "chat" and (
            slot is None or slot.image_revision != task.request["slot_revision"]
        ):
            task.status, task.error = "cancelled", "image_replaced"
            task.finished_at, task.input_snapshot = datetime.now(UTC), None
            return
        snapshot = task.input_snapshot or {}
        reference = (
            owned_image(db, user_id, UUID(snapshot["reference_id"]))
            if snapshot.get("reference_id")
            else None
        )
        if snapshot.get("reference_id") and reference is None:
            task.status, task.error = "failed", "reference_unavailable"
            task.finished_at, task.input_snapshot = datetime.now(UTC), None
            return
        prompt, reference_data = snapshot.get("prompt", ""), reference.data if reference else None
        task.status, task.started_at = "running", datetime.now(UTC)
    error, data = None, None
    try:
        data = await (generate or generate_image)(
            settings, engine, task_id, user_id, prompt, reference_data
        )
        data, _mime = normalize_image(data)
    except ModelBusy:
        retry = False
        with Session(engine) as db, db.begin():
            user = lock_account(db, user_id)
            task = db.get(Task, task_id)
            if task and task.status == "running":
                task.status = "pending"
                retry = True
        if retry:
            raise JobRetry(RetryDecision(retry_in={"seconds": 3})) from None
        return
    except ModelCallUnknown:
        error = "model_unknown"
    except (ModelCallFailed, ValueError):
        error = "image_failed"
    with Session(engine) as db, db.begin():
        user = lock_account(db, user_id)
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            return
        task.input_snapshot = None
        if task.status != "running":
            return
        binding = db.get(QQBinding, user_id) if user else None
        if not user or not user.active or not binding or binding.qq_id != task.request.get("qq_id"):
            task.status, task.error = "cancelled", "account_unavailable"
            task.finished_at = datetime.now(UTC)
            return
        task.finished_at = datetime.now(UTC)
        if error:
            task.status = "outcome_unknown" if error == "model_unknown" else "failed"
            task.error = error
            text = (
                "这次绘图结果暂时无法确认，已停止自动重画。原图仍保留。"
                if error == "model_unknown"
                else "这次绘图未能完成，原图仍保留。"
            )
            db.add(
                OutboundMessage(user_id=user_id, task_id=task.id, qq_id=binding.qq_id, content=text)
            )
            return
        target = task.request["target"]
        slot = (
            db.get(Chapter, task.chapter_id)
            if target == "illustration"
            else (db.get(Project, task.project_id) if target == "cover" else None)
        )
        if target != "chat" and (
            slot is None or slot.image_revision != task.request["slot_revision"]
        ):
            task.status, task.error = "cancelled", "image_replaced"
            return
        item = store_image(db, user_id, data, origin="generated", temporary=target == "chat")
        if slot:
            setattr(
                slot,
                "illustration_image_id" if target == "illustration" else "cover_image_id",
                item.id,
            )
            slot.image_revision += 1
        task.status, task.adopted_at = "completed", datetime.now(UTC)
        task.result = {"image_id": str(item.id), "cost_known": False}
        db.add(
            OutboundMessage(
                user_id=user_id,
                task_id=task.id,
                qq_id=binding.qq_id,
                content="刚才那张图画好了。"
                if target == "chat"
                else "图片画好了，已保存到对应作品。",
                private_image_id=item.id,
            )
        )


def cleanup_images(db):
    expired = select(PrivateImage.id).where(PrivateImage.expires_at <= datetime.now(UTC))
    db.execute(
        update(OutboundMessage)
        .where(
            OutboundMessage.private_image_id.in_(expired),
            OutboundMessage.status.in_(["pending", "sending"]),
        )
        .values(status="cancelled", content="", finished_at=datetime.now(UTC))
    )
    # No image history: replaced pictures are collected once no slot/reference/outbox needs them.
    db.execute(
        delete(PrivateImage).where(
            or_(
                PrivateImage.expires_at <= datetime.now(UTC),
                ~exists(select(1).where(Project.cover_image_id == PrivateImage.id))
                & ~exists(select(1).where(Project.image_reference_id == PrivateImage.id))
                & ~exists(select(1).where(Chapter.illustration_image_id == PrivateImage.id))
                & ~exists(select(1).where(ImagePreference.lisa_reference_id == PrivateImage.id))
                & ~exists(
                    select(1).where(
                        OutboundMessage.private_image_id == PrivateImage.id,
                        OutboundMessage.status.in_(["pending", "sending"]),
                    )
                )
                & PrivateImage.expires_at.is_(None),
            )
        )
    )
