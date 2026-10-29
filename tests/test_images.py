import asyncio
import base64
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from PIL import Image
from procrastinate.exceptions import JobRetry
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_chat import ingest
from test_works import HEADERS
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.chat import claim_outbound, execute_chat, finish_delivery
from lisa.dialogue import Conversation, Dialogue
from lisa.images import (
    ImageAction,
    apply_image_action,
    cleanup_images,
    execute_image,
    normalize_image,
    store_image,
)
from lisa.model_calls import ModelBusy, ModelCallUnknown, _finish, _reserve
from lisa.models import (
    Account,
    BudgetDay,
    ChatMessage,
    ImagePreference,
    ModelCall,
    OutboundMessage,
    PrivateImage,
    Project,
    Task,
)
from lisa.shared_retrieval import outbound_segments


def png(color="green"):
    buffer = io.BytesIO()
    Image.new("RGB", (48, 64), color).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def image_settings(settings):
    return settings.model_copy(
        update={
            "image_model": "synthetic-image",
            "image_base_url": "https://example.invalid/v1",
            "image_api_key": SecretStr("synthetic-image-key"),
        }
    )


def action(project=None, **updates):
    return ImageAction(
        target="cover" if project else "chat",
        project_id=UUID(project["id"]) if project else None,
        goal="雨夜车站的一盏灯",
        requested=True,
        evidence="请画一张图",
        **updates,
    )


def commission(db, settings, user_id, proposal):
    source = Task(user_id=user_id, kind="chat", status="completed")
    db.add(source)
    db.flush()
    notice = apply_image_action(
        db, settings, db.get(Account, user_id), source, proposal, [proposal.evidence]
    )
    db.commit()
    task = db.scalar(select(Task).where(Task.kind == "image").order_by(Task.created_at.desc()))
    return task, notice


async def fake_image(*_args):
    return png()


def mock_image_chat(monkeypatch, proposal, text="本轮内部回复正文。"):
    result = Dialogue(reply=text, opinions=[], commission=None, image_action=proposal)
    generate = AsyncMock(return_value=result)
    monkeypatch.setattr("lisa.chat.generate_conversation", generate)
    monkeypatch.setattr("lisa.chat.generate_dialogue", generate)
    return generate


def test_private_upload_validates_and_never_calls_model(client, db, project, chapter, owner):
    payload = {"content_base64": base64.b64encode(png()).decode()}
    cover = client.put(f"/api/projects/{project['id']}/cover", json=payload, headers=HEADERS)
    assert cover.status_code == 200, cover.text
    image_id = cover.json()["image_id"]
    image = client.get(f"/api/private-images/{image_id}")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert client.get(f"/api/projects/{project['id']}").json()["cover_image_id"] == image_id
    result = client.put(f"/api/chapters/{chapter}/illustration", json=payload, headers=HEADERS)
    assert result.status_code == 200
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
    assert (
        client.put(
            f"/api/projects/{project['id']}/cover",
            json={"content_base64": base64.b64encode(b"not an image").decode()},
            headers=HEADERS,
        ).status_code
        == 400
    )
    assert client.get(f"/api/projects/{project['id']}/cover").json()["image_id"] == image_id
    client.post("/api/auth/logout", headers=HEADERS)
    assert client.get(f"/api/private-images/{image_id}").status_code == 401


def test_cross_account_and_public_links_cannot_read_private_images(
    client, db, owner, project, invites
):
    first = client.put(
        f"/api/projects/{project['id']}/cover",
        json={"content_base64": base64.b64encode(png()).decode()},
        headers=HEADERS,
    ).json()["image_id"]
    share = client.post(
        f"/api/projects/{project['id']}/share", json={"enabled": True}, headers=HEADERS
    )
    assert first not in share.text
    from urllib.parse import urlsplit

    public_path = urlsplit(share.json()["url"]).path
    public = client.get(public_path)
    assert public.status_code == 200 and "/api/private-images/" not in public.text
    client.post("/api/auth/logout", headers=HEADERS)
    client.post(
        "/api/auth/register",
        json={
            "username": "other_image_reader",
            "password": "synthetic-password-2026",
            "invite_code": invites[1],
        },
        headers=HEADERS,
    )
    # Uninitialized accounts cannot access work images either.
    other = db.scalar(select(Account).where(Account.username == "other_image_reader"))
    from lisa.models import Persona, QQBinding

    db.add_all(
        [
            QQBinding(user_id=other.id, qq_id="720000009"),
            Persona(user_id=other.id, name="合成", prompt="合成", version=1),
        ]
    )
    db.commit()
    assert client.get(f"/api/private-images/{first}").status_code == 404
    assert client.get(f"/api/projects/{project['id']}/cover").status_code == 404
    assert (
        client.put(
            f"/api/projects/{project['id']}/cover",
            json={"content_base64": base64.b64encode(png()).decode()},
            headers=HEADERS,
        ).status_code
        == 404
    )


@pytest.mark.asyncio
async def test_commission_generates_in_independent_task_and_saves_correct_slot(
    db, engine, owner, project, chapter, image_settings
):
    proposal = action(project).model_copy(update={"target": "illustration", "chapter_id": chapter})
    task, notice = commission(db, image_settings, owner, proposal)
    assert "安排绘图" in notice
    assert task.kind == "image" and task.queue_job_id
    assert not db.scalar(select(OutboundMessage.id))
    await execute_image(image_settings, engine, task.id, generate=fake_image)
    db.expire_all()
    task = db.get(Task, task.id)
    assert task.status == "completed" and task.input_snapshot is None
    assert task.project_id == UUID(project["id"])
    from lisa.models import Chapter

    image_id = db.get(Chapter, chapter).illustration_image_id
    assert image_id
    message = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task.id))
    assert message.private_image_id == image_id
    segments = outbound_segments(db, message)
    assert [segment["type"] for segment in segments] == ["image"]
    assert "base64://" in segments[0]["data"]["file"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["upload", "cancel", "delete", "binding"])
async def test_late_image_does_not_overwrite_or_revive(
    client, db, engine, image_settings, owner, project, change
):
    task, _ = commission(db, image_settings, owner, action(project))
    task_id = task.id

    async def delayed(*_args):
        if change == "upload":
            response = client.put(
                f"/api/projects/{project['id']}/cover",
                json={"content_base64": base64.b64encode(png("red")).decode()},
                headers=HEADERS,
            )
            assert response.status_code == 200
        elif change == "cancel":
            assert client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
        elif change == "delete":
            assert (
                client.request(
                    "DELETE",
                    "/api/auth/account",
                    json={"password": "synthetic-password-2026"},
                    headers=HEADERS,
                ).status_code
                == 204
            )
        else:
            from lisa.models import QQBinding

            with Session(engine) as other, other.begin():
                other.get(QQBinding, owner).qq_id = "720000099"
        return png()

    await execute_image(image_settings, engine, task_id, generate=delayed)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task is None or task.status == "cancelled"
    assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
    if change == "upload":
        cover = db.get(PrivateImage, db.get(Project, UUID(project["id"])).cover_image_id)
        assert Image.open(io.BytesIO(cover.data)).getpixel((0, 0)) == (255, 0, 0)
    elif change == "delete":
        assert db.scalar(select(func.count()).select_from(PrivateImage)) == 0


@pytest.mark.asyncio
async def test_new_message_keeps_ready_picture_and_send_retry_never_redraws(
    db, engine, image_settings, owner, project
):
    task, _ = commission(db, image_settings, owner, action(project))
    generator = AsyncMock(return_value=png())
    await execute_image(image_settings, engine, task.id, generate=generator)
    ingest(db, "image-new-topic", qq_id="720000001", content="换个话题")
    db.expire_all()
    message = claim_outbound(db, None, settings=image_settings)
    assert message and message.task_id == task.id
    db.commit()
    finish_delivery(db, message.id, "failed")
    db.commit()
    assert db.get(OutboundMessage, message.id).status == "pending"
    await execute_image(image_settings, engine, task.id, generate=generator)
    assert generator.await_count == 1
    with db.begin_nested():
        db.get(OutboundMessage, message.id).status = "sending"
    db.commit()
    finish_delivery(db, message.id, "unknown")
    db.commit()
    assert db.get(OutboundMessage, message.id).status == "unknown"


@pytest.mark.asyncio
async def test_unknown_is_terminal_and_no_automatic_regeneration(db, engine, image_settings, owner):
    task, _ = commission(db, image_settings, owner, action())
    generator = AsyncMock(side_effect=ModelCallUnknown())
    await execute_image(image_settings, engine, task.id, generate=generator)
    await execute_image(image_settings, engine, task.id, generate=generator)
    db.expire_all()
    assert db.get(Task, task.id).status == "outcome_unknown"
    assert db.get(Task, task.id).input_snapshot is None
    assert generator.await_count == 1


@pytest.mark.asyncio
async def test_busy_before_provider_requeues_same_task(db, engine, image_settings, owner):
    task, _ = commission(db, image_settings, owner, action())
    with pytest.raises(JobRetry):
        await execute_image(
            image_settings, engine, task.id, generate=AsyncMock(side_effect=ModelBusy())
        )
    db.expire_all()
    assert db.get(Task, task.id).status == "pending"
    await execute_image(image_settings, engine, task.id, generate=fake_image)
    db.expire_all()
    assert db.get(Task, task.id).status == "completed"


def test_budget_exempt_description_and_generation_do_not_consume_text_budget(
    db, engine, image_settings, owner
):
    source = Task(user_id=owner, kind="chat", status="running")
    db.add(source)
    db.add(BudgetDay(day=datetime.now(UTC).date(), spent_usd=Decimal("100")))
    db.commit()
    for _index in range(5):
        call_id = _reserve(
            image_settings,
            engine,
            source.id,
            owner,
            Decimal("30"),
            configuration={"purpose": "image_description"},
            budget_exempt=True,
        )
        _finish(engine, call_id, state="succeeded", charged=Decimal("20"))
    db.expire_all()
    day = db.get(BudgetDay, datetime.now(UTC).date())
    assert day.spent_usd == 100 and day.reserved_usd == 0
    # B16 text tasks ignore the old consumption gate and still keep accounting.
    other = Task(user_id=owner, kind="scene", status="running")
    db.add(other)
    db.commit()
    text_call = _reserve(image_settings, engine, other.id, owner, Decimal("0.01"))
    _finish(engine, text_call, state="succeeded", charged=Decimal("0.01"))
    db.expire_all()
    assert db.get(BudgetDay, datetime.now(UTC).date()).spent_usd == Decimal("100.01")
    call_id = _reserve(
        image_settings,
        engine,
        source.id,
        owner,
        Decimal(0),
        configuration={"purpose": "image_generation"},
        budget_exempt=True,
    )
    # Image transport occupies its own slot and cannot block normal chat calls.
    regular = _reserve(image_settings, engine, source.id, owner, Decimal("0.01"))
    _finish(engine, regular, state="failed", charged=Decimal(0))
    _finish(engine, call_id, state="unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["请画一张图", "给我做张头像"])
async def test_exhausted_text_budget_still_accepts_drawing_turn(
    db, engine, image_settings, owner, project, monkeypatch, content
):
    source_id = ingest(db, "exempt-image", qq_id="720000001", content=content)
    db.add(BudgetDay(day=datetime.now(UTC).date(), spent_usd=Decimal("999")))
    db.commit()
    result = Dialogue(
        reply="本轮内部回复正文。", opinions=[], commission=None,
        image_action=action(project).model_copy(update={"evidence": content}),
    )
    provider = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_text=json.dumps(result.model_dump(mode="json")),
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    ))
    monkeypatch.setattr("lisa.model_calls._provider_request", provider)
    configured = image_settings.model_copy(update={"model_api_key": SecretStr("synthetic")})
    await execute_chat(configured, engine, source_id)
    db.expire_all()
    assert db.get(Task, source_id).status == "completed"
    assert db.scalar(select(Task.id).where(Task.kind == "image"))
    assert not db.scalar(select(ChatMessage.id))
    assert provider.await_count == 1
    call = db.scalar(select(ModelCall).where(ModelCall.task_id == source_id))
    assert call.state == "succeeded" and not call.budget_exempt
    assert call.configuration["schema"] == "Dialogue"
    assert call.configuration.get("purpose") != "image_description"
    assert call.charged_usd > 0
    assert db.get(BudgetDay, datetime.now(UTC).date()).spent_usd > Decimal("999")


def test_reference_is_explicit_private_and_required_for_lisa(db, owner, image_settings):
    proposal = action(depicts_lisa=True)
    task, notice = commission(db, image_settings, owner, proposal)
    assert task is None and "人设参考" in notice
    source = Task(user_id=owner, kind="chat", status="completed")
    db.add(source)
    db.flush()
    reference = proposal.model_copy(update={"operation": "reference"})
    notice = apply_image_action(
        db, image_settings, db.get(Account, owner), source, reference, [reference.evidence], png()
    )
    db.commit()
    assert "已保存" in notice
    assert db.get(ImagePreference, owner).lisa_reference_id
    task, _ = commission(db, image_settings, owner, proposal)
    assert task.input_snapshot["reference_id"]
    assert "LISA出镜" in task.input_snapshot["prompt"]


def test_inflight_dedup_and_explicit_targets(db, owner, image_settings, project):
    task, _ = commission(db, image_settings, owner, action(project))
    another, notice = commission(db, image_settings, owner, action(project))
    assert another.id == task.id and "重复" in notice
    assert db.scalar(select(func.count()).select_from(Task).where(Task.kind == "image")) == 1
    invalid = action(project).model_copy(update={"project_id": uuid4()})
    _, notice = commission(db, image_settings, owner, invalid)
    assert "哪部作品" in notice


def test_expired_chat_images_cleaned_but_reference_and_work_picture_remain(
    db, image_settings, owner, project
):
    ephemeral = store_image(db, owner, png(), origin="generated", temporary=True)
    ephemeral.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    permanent = store_image(db, owner, png(), origin="upload")
    reference = store_image(db, owner, png(), origin="reference")
    db.get(Project, UUID(project["id"])).cover_image_id = permanent.id
    db.add(ImagePreference(user_id=owner, lisa_reference_id=reference.id))
    source = Task(user_id=owner, kind="image", status="completed")
    db.add(source)
    db.flush()
    db.add(
        OutboundMessage(
            user_id=owner,
            task_id=source.id,
            qq_id="720000001",
            content="图好了",
            private_image_id=ephemeral.id,
        )
    )
    ids = ephemeral.id, permanent.id, reference.id
    db.commit()
    cleanup_images(db)
    db.commit()
    db.expire_all()
    assert db.get(PrivateImage, ids[0]) is None
    assert db.get(PrivateImage, ids[1]) and db.get(PrivateImage, ids[2])
    assert db.scalar(select(OutboundMessage)).status == "cancelled"


@pytest.mark.asyncio
async def test_replaced_delivered_work_image_is_not_kept_as_history(
    client, db, engine, image_settings, owner, project
):
    task, _ = commission(db, image_settings, owner, action(project))
    await execute_image(image_settings, engine, task.id, generate=fake_image)
    db.expire_all()
    message = db.scalar(select(OutboundMessage))
    old_id = message.private_image_id
    message.status = "sent"
    db.commit()
    response = client.put(
        f"/api/projects/{project['id']}/cover",
        json={"content_base64": base64.b64encode(png("red")).decode()},
        headers=HEADERS,
    )
    assert response.status_code == 200
    cleanup_images(db)
    db.commit()
    db.expire_all()
    assert db.get(PrivateImage, old_id) is None
    assert db.get(PrivateImage, UUID(response.json()["image_id"]))


@pytest.mark.asyncio
async def test_natural_drawings_skip_consecutive_turn_but_explicit_has_no_cooldown(
    db, engine, owner, image_settings
):
    async def natural(*_args):
        return Conversation(
            reply="给话题配个小点缀。",
            image_action=action().model_copy(update={"requested": False, "evidence": ""}),
        )

    for index in range(2):
        source = ingest(db, f"natural-{index}", qq_id="720000001", content="合成闲聊")
        await execute_chat(image_settings, engine, source, generate=natural)
        db.expire_all()
        tasks = db.scalars(select(Task).where(Task.kind == "image")).all()
        assert len(tasks) == 1
        if index == 0:
            await execute_image(image_settings, engine, tasks[0].id, generate=fake_image)
    task, notice = commission(db, image_settings, owner, action())
    assert task.status == "pending" and "已安排" in notice
    assert db.scalar(select(func.count()).select_from(Task).where(Task.kind == "image")) == 2


@pytest.mark.parametrize("body", [b"not-a-png", b"<svg></svg>", b"", b"GIF89a"])
def test_invalid_images_rejected(body):
    with pytest.raises(ValueError):
        normalize_image(body)


def test_interrupted_image_is_unknown_and_does_not_change_text_totals(
    db, engine, owner, image_settings
):
    from sqlalchemy import text

    from lisa.recovery import recover_tasks

    task, _ = commission(db, image_settings, owner, action())
    task.status = "running"
    db.execute(
        text("UPDATE procrastinate_jobs SET status='doing' WHERE id=:id"), {"id": task.queue_job_id}
    )
    db.commit()
    call_id = _reserve(
        image_settings,
        engine,
        task.id,
        owner,
        Decimal(0),
        budget_exempt=True,
        configuration={"purpose": "image_generation"},
    )
    recover_tasks(engine)
    db.expire_all()
    assert db.get(Task, task.id).status == "outcome_unknown"
    assert db.get(Task, task.id).input_snapshot is None
    assert db.get(ModelCall, call_id).state == "unknown"
    assert db.scalar(select(BudgetDay)).spent_usd == 0
    assert recover_tasks(engine) == 0


@pytest.mark.asyncio
async def test_real_sdk_shapes_use_base64_and_multipart_without_retry(
    db, engine, image_settings, owner, monkeypatch
):
    from types import SimpleNamespace

    from lisa.images import generate_image

    captured = []
    response = SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(png()).decode())], usage=None
    )

    async def generation(**kwargs):
        captured.append(kwargs)
        return response

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            kwargs["http_client"].close = AsyncMock()
            self.http = kwargs["http_client"]
            self.images = SimpleNamespace(generate=generation, edit=generation)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            await self.http.aclose()

    monkeypatch.setattr("lisa.images.AsyncOpenAI", Client)
    for reference in (None, png()):
        task = Task(user_id=owner, kind="image", status="running")
        db.add(task)
        db.commit()
        assert await generate_image(image_settings, engine, task.id, owner, "合成图", reference)
    assert "image" not in captured[0]
    assert captured[1]["image"][0] == "reference.png"
    assert all(call["n"] == 1 and call["output_format"] == "png" for call in captured)
    db.expire_all()
    calls = db.scalars(select(ModelCall)).all()
    assert all(c.budget_exempt and c.configuration["cost_known"] is False for c in calls)


@pytest.mark.asyncio
async def test_natural_picture_completes_text_before_drawing(db, engine, owner, image_settings):
    source_id = ingest(db, "natural-picture", qq_id="720000001", content="想看一颗小星星")

    async def reply(*_args):
        return Conversation(
            reply="给你一颗小星星。",
            image_action=action().model_copy(
                update={"requested": False, "evidence": "", "goal": "一颗星星落在纸上"}
            ),
        )

    await execute_chat(image_settings, engine, source_id, generate=reply)
    db.expire_all()
    assert db.get(Task, source_id).status == "completed"
    image_task = db.scalar(select(Task).where(Task.kind == "image"))
    assert image_task.status == "pending"
    assert db.scalar(select(OutboundMessage)).private_image_id is None
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "720000099"])
async def test_late_ordinary_chat_cannot_draw_after_binding_changed(
    db, engine, owner, image_settings, replacement
):
    from lisa.models import QQBinding

    source = ingest(db, "old-binding-picture", qq_id="720000001", content="合成闲聊")

    async def reply(*_args):
        with Session(engine) as other, other.begin():
            binding = other.get(QQBinding, owner)
            if replacement:
                binding.qq_id = replacement
            else:
                other.delete(binding)
        return Conversation(
            reply="我们接着聊。",
            image_action=action().model_copy(update={"requested": False, "evidence": ""}),
        )

    await execute_chat(image_settings, engine, source, generate=reply)
    db.expire_all()
    assert not db.scalar(select(Task.id).where(Task.kind == "image"))


@pytest.mark.asyncio
async def test_large_image_catalog_does_not_block_ordinary_chat(
    db, engine, owner, image_settings, monkeypatch
):
    monkeypatch.setattr("lisa.images.image_context", lambda *_args: "大图册" * 50_000)
    source = ingest(db, "large-image-catalog", qq_id="720000001", content="晚安")

    async def reply(_settings, _engine, _task_id, _user_id, instructions, _messages):
        assert "大图册" not in instructions
        return Conversation(reply="晚安。")

    await execute_chat(image_settings, engine, source, generate=reply)
    db.expire_all()
    assert db.get(Task, source).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["chat", "cover", "illustration"])
async def test_explicit_drawing_can_reply_with_one_image_and_no_acknowledgement(
    db, engine, image_settings, owner, project, chapter, monkeypatch, target
):
    proposal = action(project if target != "chat" else None).model_copy(
        update={
            "target": target,
            "chapter_id": chapter if target == "illustration" else None,
            "send_text_reply": False,
            "caption": "",
        }
    )
    source = ingest(db, "only-image", qq_id="720000001", content="请画一张图，只发图片不用文字")
    generate = mock_image_chat(monkeypatch, proposal)
    configured = image_settings.model_copy(update={"model_api_key": SecretStr("synthetic")})
    await execute_chat(configured, engine, source)
    assert generate.await_count == 1
    db.expire_all()
    assert db.get(Task, source).status == "completed"
    assert db.get(Task, source).result["outbound_count"] == 0
    assert not db.scalar(select(OutboundMessage.id))
    image_task = db.scalar(select(Task).where(Task.kind == "image"))
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    db.expire_all()
    messages = db.scalars(select(OutboundMessage)).all()
    assert len(messages) == 1 and messages[0].content == ""
    assert [item["type"] for item in outbound_segments(db, messages[0])] == ["image"]
    assert not db.scalar(select(ChatMessage.id))
    assert db.get(Task, image_task.id).input_snapshot is None
    assert "caption" not in db.get(Task, image_task.id).request


@pytest.mark.asyncio
async def test_natural_image_only_reply_does_not_adopt_unsent_text(
    db, engine, image_settings, owner, monkeypatch
):
    source = ingest(db, "natural-only-image", qq_id="720000001", content="合成闲聊")

    async def reply(*_args):
        return Conversation(
            reply="这段内部文字不应发送或成为已分享事实。",
            image_action=action().model_copy(
                update={"requested": False, "evidence": "", "send_text_reply": False}
            ),
        )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Unsent reply text must not be adopted")

    monkeypatch.setattr("lisa.chat.adopt_life_events", unexpected)
    await execute_chat(image_settings, engine, source, generate=reply)
    db.expire_all()
    assert db.get(Task, source).result["outbound_count"] == 0
    assert not db.scalar(select(OutboundMessage.id))
    image_task = db.scalar(select(Task).where(Task.kind == "image"))
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    db.expire_all()
    assert [item["type"] for item in outbound_segments(db, db.scalar(select(OutboundMessage)))] == [
        "image"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", ["", "送你一片安静的夜色。"])
async def test_optional_caption_is_delivered_only_when_supplied(
    db, engine, image_settings, owner, caption
):
    image_task, _ = commission(db, image_settings, owner, action(caption=caption))
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    db.expire_all()
    message = db.scalar(select(OutboundMessage))
    assert message.content == caption
    assert [item["type"] for item in outbound_segments(db, message)] == (
        ["text", "image"] if caption else ["image"]
    )
    assert db.get(Task, image_task.id).input_snapshot is None
    assert "caption" not in db.get(Task, image_task.id).request


@pytest.mark.asyncio
async def test_requested_poem_can_accompany_picture_without_an_extra_ack(
    db, engine, image_settings, owner, monkeypatch
):
    source = ingest(db, "poem-with-image", qq_id="720000001", content="请画一张图，配一句短诗")
    mock_image_chat(
        monkeypatch, action(send_text_reply=False, caption="落日沉入海，晚风吻过浪。")
    )
    configured = image_settings.model_copy(update={"model_api_key": SecretStr("synthetic")})
    await execute_chat(configured, engine, source)
    db.expire_all()
    assert not db.scalar(select(OutboundMessage.id))
    image_task = db.scalar(select(Task).where(Task.kind == "image"))
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    db.expire_all()
    messages = db.scalars(select(OutboundMessage)).all()
    assert len(messages) == 1 and messages[0].content == "落日沉入海，晚风吻过浪。"
    assert [item["type"] for item in outbound_segments(db, messages[0])] == ["text", "image"]


@pytest.mark.asyncio
async def test_old_pending_image_task_keeps_legacy_delivery_text(db, engine, image_settings, owner):
    image_task, _ = commission(db, image_settings, owner, action())
    snapshot = dict(image_task.input_snapshot)
    snapshot.pop("caption")
    image_task.input_snapshot = snapshot
    db.commit()
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    db.expire_all()
    assert db.scalar(select(OutboundMessage)).content == "刚才那张图画好了。"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_rejected_image_only_request_still_explains_the_problem(
    db, engine, image_settings, owner, monkeypatch, explicit
):
    proposal = action(send_text_reply=False).model_copy(update={"goal": ""})
    source = ingest(db, "rejected-image", qq_id="720000001", content="请画一张图，只发图片")
    mock_image_chat(monkeypatch, proposal, "需要先明确画面。")

    async def reply(*_args):
        return Conversation(reply="需要先明确画面。", image_action=proposal)

    configured = image_settings.model_copy(update={"model_api_key": SecretStr("synthetic")})
    await execute_chat(configured, engine, source, generate=None if explicit else reply)
    db.expire_all()
    assert not db.scalar(select(Task.id).where(Task.kind == "image"))
    assert not db.get(Task, source).result.get("suppress_text_reply")
    message = db.scalar(select(OutboundMessage))
    assert "请再说明这张图要表现的场景" in message.content
    assert [item["type"] for item in outbound_segments(db, message)] == ["text"]


@pytest.mark.asyncio
async def test_missing_reference_reports_failure_without_an_ack(
    db, engine, image_settings, owner
):
    image_task, _ = commission(db, image_settings, owner, action(send_text_reply=False))
    image_task.input_snapshot = {**image_task.input_snapshot, "reference_id": str(uuid4())}
    db.commit()
    generate = AsyncMock()
    await execute_image(image_settings, engine, image_task.id, generate=generate)
    db.expire_all()
    assert generate.await_count == 0
    assert db.get(Task, image_task.id).error == "reference_unavailable"
    assert db.get(Task, image_task.id).input_snapshot is None
    message = db.scalar(select(OutboundMessage))
    assert "参考图已不可用" in message.content
    assert [item["type"] for item in outbound_segments(db, message)] == ["text"]


@pytest.mark.asyncio
async def test_image_only_failure_still_reports_error(db, engine, image_settings, owner):
    image_task, _ = commission(db, image_settings, owner, action(send_text_reply=False))
    await execute_image(
        image_settings, engine, image_task.id, generate=AsyncMock(side_effect=ModelCallUnknown())
    )
    db.expire_all()
    message = db.scalar(select(OutboundMessage))
    assert "无法确认" in message.content
    assert [item["type"] for item in outbound_segments(db, message)] == ["text"]


@pytest.mark.asyncio
async def test_onebot_receives_an_image_only_segment_and_no_empty_text(
    db, engine, image_settings, owner
):
    from lisa.gateway import deliver_outbox

    image_task, _ = commission(db, image_settings, owner, action(send_text_reply=False))
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    sent = asyncio.Event()

    class Connection:
        async def call(self, method, params):
            assert method == "send_private_msg"
            assert [item["type"] for item in params["message"]] == ["image"]
            assert params["message"][0]["data"]["file"].startswith("base64://")
            sent.set()
            return {"message_id": 987654}

    sender = asyncio.create_task(
        deliver_outbox(Connection(), engine, None, settings=image_settings)
    )
    try:
        await asyncio.wait_for(sent.wait(), timeout=5)
    finally:
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
    db.expire_all()
    assert db.scalar(select(OutboundMessage)).status == "sent"


@pytest.mark.asyncio
async def test_missing_image_cancels_delivery_without_sending_empty_payload(
    db, engine, image_settings, owner
):
    from lisa.gateway import deliver_outbox

    image_task, _ = commission(db, image_settings, owner, action(send_text_reply=False))
    await execute_image(image_settings, engine, image_task.id, generate=fake_image)
    db.expire_all()
    message = db.scalar(select(OutboundMessage))
    db.get(PrivateImage, message.private_image_id).expires_at = datetime.now(UTC) - timedelta(
        seconds=1
    )
    db.commit()
    connection = type("Connection", (), {"call": AsyncMock()})()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            deliver_outbox(connection, engine, None, settings=image_settings), 0.2
        )
    db.expire_all()
    assert connection.call.await_count == 0
    assert db.get(OutboundMessage, message.id).status == "cancelled"
