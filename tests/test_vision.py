import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from procrastinate.exceptions import JobRetry
from pydantic import SecretStr
from sqlalchemy import select
from test_chat import create_user

from lisa import model_calls, vision
from lisa.chat import execute_chat, ingest_message
from lisa.dialogue import Conversation
from lisa.gateway import parse_private_message
from lisa.memory import MemoryCandidate
from lisa.model_calls import ModelBusy
from lisa.models import Account, ChatMessage, Memory, ModelCall, Task

IMAGE_URL = "https://multimedia.nt.qq.com/download?synthetic-secret=never-log"
CAPTION = "图片中是一座雨夜车站，站台上有一个红色信箱。"


@pytest.fixture
def visual_settings(settings):
    return settings.model_copy(
        update={
            "vision_model": "synthetic-vision",
            "vision_base_url": "https://vision.invalid/v1",
            "vision_api_key": SecretStr("synthetic-vision-key"),
            "vision_input_per_million": Decimal("0.8"),
            "vision_output_per_million": Decimal("2.7"),
        }
    )


@pytest.fixture
def image_message(db):
    user_id = create_user(db)
    task_id = ingest_message(
        db,
        bot_id="910000001",
        message_id="image-1",
        qq_id="710000001",
        sent_at=datetime.now(UTC),
        content="看看这张图",
        kind="image",
        image_urls=[IMAGE_URL],
    )
    db.commit()
    message = db.scalar(select(ChatMessage).where(ChatMessage.task_id == task_id))
    return user_id, task_id, message.id


@pytest.fixture
def vision_sdk(monkeypatch, image_message):
    attempts = []
    configurations = []
    after_request = []

    class FakeClient:
        def __init__(self, **kwargs):
            configurations.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def create(self, **kwargs):
            attempts.append(kwargs)
            for callback in after_request:
                callback()
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "observations": [
                                        {
                                            "source_id": str(image_message[2]),
                                            "description": CAPTION,
                                        }
                                    ]
                                }
                            )
                        ),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=60, completion_tokens=10),
            )

    monkeypatch.setattr(model_calls, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(vision, "download_image", lambda _url: "data:image/png;base64,synthetic")
    return attempts, configurations, after_request


def test_mixed_image_text_retains_text_without_becoming_a_service_command():
    event = {
        "post_type": "message",
        "message_type": "private",
        "self_id": "910000001",
        "user_id": "710000001",
        "message_id": "123",
        "time": datetime.now(UTC).timestamp(),
        "message": [
            {"type": "text", "data": {"text": "绑定"}},
            {"type": "image", "data": {"url": IMAGE_URL}},
        ],
    }
    message = parse_private_message(event, bot_id="910000001", allow_qq=None)
    assert message.text == "绑定" and message.kind == "image"
    assert message.image_urls == (IMAGE_URL,)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "file:///C:/secret",
        "https://qpic.cn.evil.invalid/a",
        "https://user:password@gchat.qpic.cn/a",
        "https://gchat.qpic.cn:8000/a",
    ],
)
def test_image_download_rejects_non_qq_locations(url):
    assert vision.download_image(url) is None


@pytest.mark.asyncio
async def test_image_description_promotes_task_memory_and_erases_transient_source(
    visual_settings,
    engine,
    db,
    image_message,
    vision_sdk,
):
    user_id, task_id, message_id = image_message

    async def generate(*args):
        text = args[-1][-1]["content"]
        assert CAPTION in text and "模型" in text
        assert IMAGE_URL not in text and "base64" not in text
        return Conversation(
            reply="这个雨夜车站很适合悬疑故事。",
            memories=[
                MemoryCandidate(
                    content="用户分享的图片里有雨夜车站和红色信箱。",
                    kind="fact",
                    evidence="图片",
                    allow_writing=True,
                )
            ],
        )

    await execute_chat(visual_settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(ChatMessage, message_id) is None
    memory = db.scalar(select(Memory).where(Memory.user_id == user_id))
    assert memory.source_message_id is None and memory.source_task_id == task_id
    assert memory.kind == "inference" and not memory.allow_writing and "图片" in memory.basis
    calls = list(db.scalars(select(ModelCall)))
    assert len(calls) == 1 and calls[0].configuration["api"] == "vision"
    assert calls[0].charged_usd == Decimal("0.000075")
    assert vision_sdk[1][0]["base_url"] == "https://vision.invalid/v1"
    assert vision_sdk[1][0]["api_key"] == "synthetic-vision-key"
    assert vision_sdk[1][0]["max_retries"] == 0

    followup = ingest_message(
        db,
        bot_id="910000001",
        message_id="followup",
        qq_id="710000001",
        sent_at=datetime.now(UTC),
        content="刚才的图片是什么？",
    )
    db.commit()

    async def remember(*args):
        assert memory.content in args[-2]
        assert len(args[-1]) == 1 and args[-1][0]["role"] == "user"
        assert all(CAPTION not in item["content"] for item in args[-1])
        assert IMAGE_URL not in args[-2] and "base64" not in args[-2]
        return "是雨夜车站。"

    await execute_chat(visual_settings, engine, followup, generate=remember)
    assert len(vision_sdk[0]) == 1
    db.expire_all()
    assert db.scalar(select(ChatMessage)) is None
    assert db.get(Memory, memory.id).active
    assert IMAGE_URL not in str(db.get(Task, task_id).input_snapshot)
    assert CAPTION not in str(db.get(Task, task_id).input_snapshot)


@pytest.mark.asyncio
async def test_deleted_source_during_vision_cannot_be_restored(
    visual_settings,
    engine,
    db,
    image_message,
    vision_sdk,
):
    user_id, task_id, message_id = image_message

    def delete_source():
        # Simulate transient inbox invalidation while the vision request is in flight.
        # The removed history-management API must not be reintroduced for this race.
        db.delete(db.get(ChatMessage, message_id))
        db.get(Account, user_id).memory_revision += 1
        db.commit()

    vision_sdk[2].append(delete_source)

    async def forbidden(*_args):
        raise AssertionError("Changed source must not reach chat generation")

    await execute_chat(visual_settings, engine, task_id, generate=forbidden)
    db.expire_all()
    assert db.get(Task, task_id).error == "context_changed"
    assert db.get(ChatMessage, message_id) is None
    assert db.scalar(select(Memory)) is None


@pytest.mark.asyncio
async def test_chat_slot_retry_reuses_saved_image_description(
    visual_settings,
    engine,
    db,
    image_message,
    vision_sdk,
):
    attempts = []

    async def generate(*_args):
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelBusy("synthetic occupied slot")
        return "图片已经看过了。"

    with pytest.raises(JobRetry):
        await execute_chat(visual_settings, engine, image_message[1], generate=generate)
    db.expire_all()
    source = db.get(ChatMessage, image_message[2])
    assert source.image_description == CAPTION and source.image_urls == []
    await execute_chat(visual_settings, engine, image_message[1], generate=generate)
    assert len(vision_sdk[0]) == 1
    db.expire_all()
    assert db.get(ChatMessage, image_message[2]) is None


@pytest.mark.asyncio
async def test_disabled_or_unreadable_image_has_explicit_fallback(
    settings,
    engine,
    db,
    image_message,
    monkeypatch,
):
    def forbidden(_url):
        raise AssertionError("Disabled vision must not download an image")

    monkeypatch.setattr(vision, "download_image", forbidden)

    async def generate(*args):
        assert "图片未读取" in args[-1][-1]["content"]
        return "这张图暂时看不到，可以用文字描述一下。"

    await execute_chat(settings, engine, image_message[1], generate=generate)
    db.expire_all()
    assert db.get(ChatMessage, image_message[2]) is None
    assert db.scalar(select(ModelCall)) is None


@pytest.mark.asyncio
async def test_invalid_vision_response_is_charged_and_not_adopted(
    visual_settings,
    engine,
    db,
    image_message,
    vision_sdk,
):
    # Invalid structured output uses the paid-call failure path; no chat or memory adoption.
    from unittest.mock import patch

    with patch.object(vision.ImageObservations, "model_validate_json", side_effect=ValueError):
        await execute_chat(visual_settings, engine, image_message[1])
    db.expire_all()
    assert db.get(Task, image_message[1]).error == "model_failed"
    assert db.scalar(select(Memory)) is None
    call = db.scalar(select(ModelCall))
    assert call.state == "failed" and call.charged_usd == Decimal("0.000075")
    assert db.get(ChatMessage, image_message[2]) is None
