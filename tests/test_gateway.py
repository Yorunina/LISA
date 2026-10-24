import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from lisa.gateway import (
    GroupMessageEvent,
    OneBotConnection,
    parse_group_message,
    parse_private_message,
    run_gateway,
    serve_connection,
)
from lisa.models import (
    Account,
    ChatMessage,
    CollectedGroup,
    GroupMessage,
    Memory,
    ModelCall,
    OutboundMessage,
    Persona,
    QQBinding,
    ServiceEvent,
    Task,
)

BOT = "10001"
USER = "20002"
GROUP = "30003"


def private_event(**changes):
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": int(BOT),
        "user_id": int(USER),
        "message_id": 123,
        "time": int(datetime.now(UTC).timestamp()),
        "message": [{"type": "text", "data": {"text": "绑定"}}],
        **changes,
    }


def group_event(**changes):
    return private_event(
        **{
            "message_type": "group",
            "sub_type": "normal",
            "group_id": int(GROUP),
            "sender": {"user_id": int(USER), "nickname": "合成昵称", "card": "合成群名片"},
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"post_type": "notice"},
        {"post_type": "message_sent"},
        {"message_type": "private"},
        {"sub_type": "anonymous"},
        {"sub_type": "notice"},
        {"sub_type": "unknown"},
        {"sub_type": None},
        {"anonymous": {"id": 1, "name": "匿名消息"}},
        {"self_id": 10002},
        {"self_id": True},
        {"group_id": None},
        {"group_id": 0},
        {"group_id": -1},
        {"group_id": True},
        {"group_id": "group"},
        {"group_id": "９９"},
        {"user_id": int(BOT)},
        {"user_id": 0},
        {"user_id": False},
        {"message_id": -(2**31) - 1},
        {"message_id": 2**31},
        {"message_id": 123.0},
        {"message_id": True},
        {"message_id": "not-an-id"},
        {"time": True},
        {"time": 0},
        {"time": float("nan")},
        {"time": float("inf")},
        {"time": "now"},
        {"time": (datetime.now(UTC) + timedelta(minutes=2)).timestamp()},
        {"sender": []},
        {"sender": {"user_id": 99999, "nickname": "wrong identity"}},
        {"message": None},
        {"message": {}},
        {"message": []},
        {"message": [None]},
        {"message": [{"type": "text", "data": {"text": 123}}]},
        {"message": [{"type": "image", "data": "invalid"}]},
        {"message": "unclosed [CQ:image,url=https://example.invalid/file"},
        {"message": "\x00"},
        {"message": "\ud800"},
        {"message": "long" * 20_000},
        {"message": [{"type": "face", "data": {}}] * 129},
    ],
)
def test_group_parser_rejects_invalid_identity_time_or_body(changes):
    assert parse_group_message(group_event(**changes), bot_id=BOT) is None


def test_group_parser_keeps_trusted_time_and_event_identity():
    now = datetime.now(UTC)
    timestamp = int((now - timedelta(seconds=299)).timestamp())
    event = parse_group_message(group_event(time=timestamp), bot_id=BOT, now=now)
    assert isinstance(event, GroupMessageEvent)
    assert (event.bot_id, event.group_id, event.message_id, event.qq_id) == (
        BOT,
        GROUP,
        "123",
        USER,
    )
    assert event.name == "合成群名片" and event.text == "绑定" and event.kind == "text"
    assert event.sent_at == datetime.fromtimestamp(timestamp, UTC)
    assert parse_group_message(group_event(time=timestamp - 2), bot_id=BOT, now=now) is None
    assert parse_group_message(group_event(group_id="9" * 100), bot_id=BOT) is None


@pytest.mark.parametrize("message_id", [-1, "-1", -(2**31), str(-(2**31)), 0, 2**31 - 1])
def test_group_signed_message_ids_are_supported(message_id):
    event = parse_group_message(group_event(message_id=message_id), bot_id=BOT)
    assert event is not None and event.message_id == str(int(message_id))


def test_group_media_has_only_placeholders_and_never_uses_private_vision(monkeypatch):
    from lisa import gateway

    def forbidden(*args, **kwargs):
        raise AssertionError("Group collection must not use private image processing")

    monkeypatch.setattr(gateway, "image_url_allowed", forbidden)
    event = parse_group_message(
        group_event(
            message=[
                {"type": "text", "data": {"text": "看看这个"}},
                {"type": "image", "data": {"url": "https://private.invalid/secret-image"}},
                {"type": "record", "data": {"file": "do-not-download"}},
                {"type": "file", "data": {"file": "do-not-open"}},
                {"type": "text", "data": {"text": "稍后再聊"}},
            ]
        ),
        bot_id=BOT,
    )
    assert event.text == "看看这个[图片][语音][文件]稍后再聊"
    assert event.kind == "nontext" and not hasattr(event, "image_urls")
    assert "secret-image" not in event.text and "do-not-download" not in event.text
    cq = parse_group_message(
        group_event(
            message="前文[CQ:image,file=private,url=https://private.invalid/img]后文[CQ:at,qq=99999]"
        ),
        bot_id=BOT,
    )
    assert cq.text == "前文[图片]后文[提及群成员]" and cq.kind == "nontext"


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_qq", [None, USER])
async def test_receive_collects_groups_only_in_normal_mode_and_never_replays_old_groups(allow_qq):
    class Socket:
        def __aiter__(self):
            async def messages():
                for event in [group_event(), group_event(message_id=124, time=0)]:
                    yield json.dumps(event)

            return messages()

    connection = OneBotConnection(Socket(), allow_qq=allow_qq, allow_old_messages=True)
    connection.bot_id = BOT
    await connection.receive()
    if allow_qq is None:
        event = await connection.events.get()
        assert isinstance(event, GroupMessageEvent) and event.message_id == "123"
    assert await connection.events.get() is None
    assert connection.events.empty()


@pytest.mark.parametrize(
    "changes",
    [
        {"message_type": "group"},
        {"post_type": "message_sent"},
        {"user_id": 30003},
        {"user_id": int(BOT)},
        {"self_id": 30003},
        {"user_id": True},
        {"message_id": None},
        {"time": 0},
        {"time": float("nan")},
        {"time": "now"},
        {"time": (datetime.now(UTC) + timedelta(minutes=2)).timestamp()},
    ],
)
def test_only_current_allowlisted_private_events(changes):
    assert parse_private_message(private_event(**changes), bot_id=BOT, allow_qq=USER) is None


def test_identity_comes_from_event_not_sender_profile():
    event = private_event(sender={"user_id": 99999, "nickname": "someone else"})
    message = parse_private_message(event, bot_id=BOT, allow_qq=USER)
    assert message.qq_id == USER
    assert message.text == "绑定"
    assert message.sent_at.tzinfo is UTC


def test_non_text_segments_cannot_become_service_commands():
    event = private_event(
        message=[
            {"type": "image", "data": {"file": "synthetic"}},
            {"type": "text", "data": {"text": "绑定"}},
        ]
    )
    assert parse_private_message(event, bot_id=BOT, allow_qq=USER).text == ""


async def send_response(socket, request, data, **changes):
    await socket.send(
        json.dumps(
            {
                "echo": request["echo"],
                "status": "ok",
                "retcode": 0,
                "data": data,
                **changes,
            }
        )
    )


@pytest.mark.asyncio
async def test_local_websocket_identity_event_routing_and_text_send():
    ready = asyncio.Event()
    requests = []

    async def server(socket):
        assert socket.request.headers["Authorization"] == "Bearer synthetic-token"
        request = json.loads(await socket.recv())
        requests.append(request)
        await send_response(socket, request, {"user_id": int(BOT)})
        request = json.loads(await socket.recv())
        requests.append(request)
        await send_response(socket, request, {"app_name": "synthetic", "app_version": "1"})
        await ready.wait()
        await socket.send("invalid-json")
        await socket.send(json.dumps(private_event(user_id=30003)))
        await socket.send(json.dumps(private_event()))
        request = json.loads(await socket.recv())
        requests.append(request)
        # Interleaving an event with the action result must not consume the result as an event.
        await socket.send(json.dumps(private_event(message_id=124)))
        await send_response(socket, request, {"message_id": 456})
        await socket.wait_closed()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(
            f"ws://127.0.0.1:{port}",
            additional_headers={
                "Authorization": "Bearer synthetic-token",
            },
        ) as socket:
            connection = OneBotConnection(socket, allow_qq=USER)
            receiver = asyncio.create_task(connection.receive())
            try:
                await connection.identify()
                ready.set()
                event = await asyncio.wait_for(connection.events.get(), 2)
                assert event.qq_id == USER and event.message_id == "123"
                assert await connection.send_private(USER, "合成服务回复") == "sent"
                assert (await connection.events.get()).message_id == "124"
                with pytest.raises(ValueError, match="allowlist"):
                    await connection.send_private("30003", "must not send")
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
    assert [r["action"] for r in requests] == [
        "get_login_info",
        "get_version_info",
        "send_private_msg",
    ]
    assert requests[-1]["params"] == {
        "user_id": int(USER),
        "message": [{"type": "text", "data": {"text": "合成服务回复"}}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("failed", "failed"),
        ("timeout", "unknown"),
        ("disconnect", "unknown"),
        ("queued", "unknown"),
        ("missing_id", "unknown"),
    ],
)
async def test_send_outcomes_do_not_blindly_repeat(outcome, expected):
    requests = []

    async def server(socket):
        request = json.loads(await socket.recv())
        requests.append(request)
        if outcome == "failed":
            await send_response(socket, request, {}, status="failed", retcode=100)
        elif outcome == "queued":
            await send_response(socket, request, {}, status="async", retcode=1)
        elif outcome == "missing_id":
            await send_response(socket, request, {})
        elif outcome == "disconnect":
            await socket.close()
        await socket.wait_closed()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            connection = OneBotConnection(socket, allow_qq=USER, timeout=0.05)
            receiver = asyncio.create_task(connection.receive())
            try:
                assert await connection.send_private(USER, "synthetic") == expected
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_gateway_requires_allowlist_before_connecting(settings):
    with pytest.raises(ValueError, match="allow-qq"):
        await run_gateway(settings.model_copy(update={"qq_test_user": None, "chat_enabled": False}))


@pytest.mark.asyncio
async def test_service_handler_routes_test_commands_and_records_delivery_in_order(
    engine, monkeypatch
):
    from lisa import gateway

    handled = []
    delivered = []

    def handle(db, **event):
        handled.append(event["message_id"])
        return event["message_id"], "synthetic service reply"

    monkeypatch.setattr(gateway, "handle_service_message", handle)
    monkeypatch.setattr(
        gateway, "record_delivery", lambda db, key, status: delivered.append((key, status))
    )

    async def server(socket):
        for data in [
            {"user_id": int(BOT)},
            {"app_name": "synthetic", "app_version": "1"},
        ]:
            await send_response(socket, json.loads(await socket.recv()), data)
        # Yield to the client's completed identity check before sending synthetic events.
        await asyncio.sleep(0.02)
        await socket.send(json.dumps(private_event(message_id=99, message="ordinary conversation")))
        for message_id in [1, 2]:
            await socket.send(json.dumps(private_event(message_id=message_id)))
        for _ in range(2):
            await send_response(socket, json.loads(await socket.recv()), {"message_id": 456})
        await socket.close()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            await asyncio.wait_for(
                serve_connection(
                    socket,
                    engine=engine,
                    allow_qq=USER,
                    web_url="http://testserver",
                ),
                2,
            )
    assert handled == ["1", "2"]
    assert delivered == [("1", "sent"), ("2", "sent")]


def signal_identified(monkeypatch):
    ready = asyncio.Event()
    identify = OneBotConnection.identify

    async def identify_and_signal(connection):
        await identify(connection)
        ready.set()

    monkeypatch.setattr(OneBotConnection, "identify", identify_and_signal)
    return ready


async def identify_client(socket, ready):
    for data in [
        {"user_id": int(BOT)},
        {"app_name": "synthetic", "app_version": "1"},
    ]:
        await send_response(socket, json.loads(await socket.recv()), data)
    await ready.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_enabled", [False, True])
async def test_normal_group_collection_is_committed_without_any_private_side_effect(
    engine,
    monkeypatch,
    chat_enabled,
):
    from lisa import gateway, group_records

    ready, drained = signal_identified(monkeypatch), asyncio.Event()
    ingested, actions = [], []
    ingest = group_records.ingest_group_message
    with Session(engine) as db, db.begin():
        db.add(CollectedGroup(bot_id=BOT, qq_group_id=GROUP, name="合成群", enabled=True))
        db.add(CollectedGroup(bot_id=BOT, qq_group_id="40004", name="未启用群", enabled=False))

    def ingest_and_signal(db, event):
        result = ingest(db, event)
        ingested.append((event.group_id, result))
        if len(ingested) == 5:
            drained.set()
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError("Passive groups must never enter a private/vision/service pipeline")

    monkeypatch.setattr(group_records, "ingest_group_message", ingest_and_signal)
    monkeypatch.setattr(gateway, "ingest_message", forbidden)
    monkeypatch.setattr(gateway, "handle_service_message", forbidden)
    monkeypatch.setattr(gateway, "image_url_allowed", forbidden)

    async def server(socket):
        await identify_client(socket, ready)
        for event in [
            group_event(message_id=-901, message="绑定"),
            group_event(message_id="-901", message="绑定"),  # Same signed ID, duplicate delivery.
            group_event(
                message_id=902,
                message=[
                    {"type": "image", "data": {"url": "https://private.invalid/image"}},
                ],
            ),
            group_event(message_id=903, group_id=40004, message="此群已停用"),
            group_event(message_id=904, group_id=50005, message="此群没有采集配置"),
        ]:
            await socket.send(json.dumps(event))
        await asyncio.wait_for(drained.wait(), 3)
        await socket.close()
        actions.extend([message async for message in socket])

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            await asyncio.wait_for(
                serve_connection(
                    socket,
                    engine=engine,
                    allow_qq=None,
                    web_url="http://testserver",
                    chat_enabled=chat_enabled,
                ),
                4,
            )
    assert actions == []  # Only identity actions occurred, no group or private response.
    assert ingested == [
        (GROUP, True),
        (GROUP, False),
        (GROUP, True),
        ("40004", False),
        ("50005", False),
    ]
    with Session(engine) as db:
        messages = list(db.scalars(select(GroupMessage).order_by(GroupMessage.message_id)))
        assert [item.message_id for item in messages] == ["-901", "902"]
        assert [(item.content, item.kind) for item in messages] == [
            ("绑定", "text"),
            ("[图片]", "nontext"),
        ]
        for model in (
            QQBinding,
            ChatMessage,
            Memory,
            ModelCall,
            Task,
            ServiceEvent,
            OutboundMessage,
        ):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_group_ingest_failure_never_falls_through_to_private_commands(engine, monkeypatch):
    from lisa import gateway, group_records

    ready = signal_identified(monkeypatch)
    private_calls = []

    def fail(db, event):
        raise RuntimeError("synthetic group storage failure")

    def private(*args, **kwargs):
        private_calls.append(kwargs)
        return None

    monkeypatch.setattr(group_records, "ingest_group_message", fail)
    monkeypatch.setattr(gateway, "ingest_message", private)
    monkeypatch.setattr(gateway, "handle_service_message", private)

    async def server(socket):
        await identify_client(socket, ready)
        await socket.send(json.dumps(group_event(message="绑定")))
        await socket.wait_closed()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            with pytest.raises(RuntimeError, match="group storage failure"):
                await asyncio.wait_for(
                    serve_connection(
                        socket,
                        engine=engine,
                        allow_qq=None,
                        web_url="http://testserver",
                    ),
                    3,
                )
    assert private_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_chat_prefix_requires_enabled_mode_and_allowlisted_sender(
    engine, monkeypatch, enabled
):
    from lisa import gateway

    ready, drained = signal_identified(monkeypatch), asyncio.Event()
    ingested = []
    services = []

    def ingest(db, **event):
        ingested.append(event)
        return "synthetic-task"

    monkeypatch.setattr(gateway, "ingest_message", ingest)

    def service(db, **event):
        services.append(event["text"])
        drained.set()
        return "synthetic-end", None

    monkeypatch.setattr(gateway, "handle_service_message", service)

    async def server(socket):
        await identify_client(socket, ready)
        for index, changes in enumerate(
            [
                {"message": "ordinary conversation"},
                {"message": "测试对话 "},
                {"message": "测试对话 wrong sender", "user_id": 30003},
                {"message": "测试对话 group", "message_type": "group"},
                {"message": "测试对话 old", "time": 0},
                {"message": "绑定", "time": 0},
                {"message": "测试对话 合成测试正文"},
                {"message": "测试引导"},
            ]
        ):
            await socket.send(json.dumps(private_event(message_id=500 + index, **changes)))
        await asyncio.wait_for(drained.wait(), 2)
        await socket.close()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            await asyncio.wait_for(
                serve_connection(
                    socket,
                    engine=engine,
                    allow_qq=USER,
                    web_url="http://testserver",
                    chat_enabled=enabled,
                ),
                2,
            )
    assert len(ingested) == 2 * int(enabled)
    assert services == ["测试引导"]
    if enabled:
        assert ingested[0]["qq_id"] == USER
        assert ingested[0]["content"] == "old"
        assert ingested[0]["sent_at"] == datetime.fromtimestamp(0, UTC)
        assert ingested[1]["content"] == "合成测试正文"


@pytest.mark.asyncio
async def test_normal_private_chat_routes_bound_users_and_keeps_commands_separate(
    engine, monkeypatch
):
    from lisa import gateway

    ready, drained = signal_identified(monkeypatch), asyncio.Event()
    with Session(engine) as db, db.begin():
        for qq_id in [USER, "30003"]:
            user = Account(username=f"normal_{qq_id}", password_hash="synthetic")
            db.add(user)
            db.flush()
            db.add(QQBinding(user_id=user.id, qq_id=qq_id))
            db.add(Persona(user_id=user.id, name="合成角色", prompt="合成性格", version=1))
    commands = []

    def service(db, **event):
        commands.append(event["text"])
        if event["text"] == "找回":
            drained.set()
        return "synthetic-command", None

    monkeypatch.setattr(gateway, "handle_service_message", service)

    async def server(socket):
        await identify_client(socket, ready)
        for index, changes in enumerate(
            [
                {"message": "普通私聊"},
                {"message": "测试对话 保留这段原文", "user_id": 30003},
                {"message": "群消息", "message_type": "group"},
                {"message": "自身消息", "user_id": int(BOT)},
                {"message": "其他机器人", "self_id": 99999},
                {
                    "message": [
                        {"type": "image", "data": {"file": "synthetic"}},
                        {"type": "text", "data": {"text": "绑定"}},
                    ]
                },
                {
                    "message": [
                        {"type": "image", "data": {"url": "https://gchat.qpic.cn/image"}},
                        {"type": "text", "data": {"text": "绑定"}},
                    ]
                },
                {
                    "user_id": 30004,
                    "message": [
                        {"type": "image", "data": {"url": "https://gchat.qpic.cn/image"}},
                        {"type": "text", "data": {"text": "绑定"}},
                    ],
                },
                {"message": "绑定"},
                {"message": "找回"},
            ]
        ):
            await socket.send(json.dumps(private_event(message_id=700 + index, **changes)))
        await asyncio.wait_for(drained.wait(), 2)
        await socket.close()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            await asyncio.wait_for(
                serve_connection(
                    socket,
                    engine=engine,
                    allow_qq=None,
                    web_url="http://testserver",
                    chat_enabled=True,
                ),
                3,
            )
    assert commands == ["", "绑定", "找回"]
    with Session(engine) as db:
        rows = list(db.scalars(select(ChatMessage).order_by(ChatMessage.event_key)))
        assert [(db.get(QQBinding, row.user_id).qq_id, row.content, row.kind) for row in rows] == [
            (USER, "普通私聊", "text"),
            ("30003", "测试对话 保留这段原文", "text"),
            (USER, "[用户发送了非文本消息，当前无法读取其内容。]", "nontext"),
            (USER, "绑定", "image"),
        ]
        assert rows[0].task_id == rows[2].task_id != rows[1].task_id
        assert rows[3].image_urls == ["https://gchat.qpic.cn/image"]


def seed_outbound(engine, *, qq_id=USER, status="pending"):
    with Session(engine) as db, db.begin():
        user = Account(username=f"gateway_{qq_id}", password_hash="synthetic")
        db.add(user)
        db.flush()
        db.add(QQBinding(user_id=user.id, qq_id=qq_id))
        task = Task(user_id=user.id, kind="chat", source="qq", status="completed")
        db.add(task)
        db.flush()
        message = OutboundMessage(
            user_id=user.id,
            task_id=task.id,
            qq_id=qq_id,
            content="合成队列回复",
            status=status,
            attempts=1 if status == "sending" else 0,
        )
        db.add(message)
        db.flush()
        return message.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("sent", "sent"),
        ("failed", "pending"),
        ("unknown", "unknown"),
        ("missing_id", "unknown"),
        ("disconnect", "unknown"),
    ],
)
async def test_outbox_persists_protocol_results_and_stops_with_connection(
    engine, monkeypatch, outcome, expected
):
    from lisa import gateway

    outbound_id = seed_outbound(engine)
    other_id = seed_outbound(engine, qq_id="30003")
    ready, finished, sender_stopped = (
        signal_identified(monkeypatch),
        asyncio.Event(),
        asyncio.Event(),
    )
    finish, deliver = gateway.finish_delivery, gateway.deliver_outbox
    requests = []

    def finish_and_signal(*args, **kwargs):
        finish(*args, **kwargs)
        finished.set()

    async def observe_sender(*args, **kwargs):
        try:
            await deliver(*args, **kwargs)
        finally:
            sender_stopped.set()

    monkeypatch.setattr(gateway, "finish_delivery", finish_and_signal)
    monkeypatch.setattr(gateway, "deliver_outbox", observe_sender)

    async def server(socket):
        await identify_client(socket, ready)
        request = json.loads(await socket.recv())
        requests.append(request)
        assert request["action"] == "send_private_msg"
        assert request["params"]["user_id"] == int(USER)
        if outcome == "disconnect":
            await socket.close()
            return
        if outcome == "sent":
            await send_response(socket, request, {"message_id": 456})
        elif outcome == "failed":
            await send_response(socket, request, {}, status="failed", retcode=100)
        elif outcome == "unknown":
            await send_response(socket, request, {}, status="async", retcode=1)
        else:
            await send_response(socket, request, {})
        await asyncio.wait_for(finished.wait(), 2)
        await socket.close()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            await asyncio.wait_for(
                serve_connection(
                    socket,
                    engine=engine,
                    allow_qq=USER,
                    web_url="http://testserver",
                    chat_enabled=True,
                ),
                3,
            )
    assert sender_stopped.is_set()
    with Session(engine) as db:
        message, other = db.get(OutboundMessage, outbound_id), db.get(OutboundMessage, other_id)
        assert message.status == expected and message.attempts == 1
        assert message.message_id == ("456" if outcome == "sent" else None)
        assert other.status == "pending" and other.attempts == 0
    assert sum(request["action"] == "send_private_msg" for request in requests) == 1


@pytest.mark.asyncio
async def test_restart_marks_unconfirmed_send_unknown_without_resending(engine, monkeypatch):
    from lisa import gateway

    outbound_id = seed_outbound(engine, status="sending")
    ready, drained = signal_identified(monkeypatch), asyncio.Event()

    def service(db, **event):
        drained.set()
        return "synthetic-end", None

    monkeypatch.setattr(gateway, "handle_service_message", service)

    async def server(socket):
        await identify_client(socket, ready)
        await socket.send(json.dumps(private_event(message="测试引导")))
        await asyncio.wait_for(drained.wait(), 2)
        await socket.close()
        assert [message async for message in socket] == []

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            await asyncio.wait_for(
                serve_connection(
                    socket,
                    engine=engine,
                    allow_qq=USER,
                    web_url="http://testserver",
                    chat_enabled=True,
                ),
                2,
            )
    with Session(engine) as db:
        message = db.get(OutboundMessage, outbound_id)
        assert message.status == "unknown" and message.attempts == 1


@pytest.mark.asyncio
async def test_sender_failure_is_propagated_instead_of_silent_delivery_stall(engine, monkeypatch):
    from lisa import gateway

    ready = signal_identified(monkeypatch)

    def failed_claim(*args, **kwargs):
        raise RuntimeError("synthetic sender failure")

    monkeypatch.setattr(gateway, "claim_outbound", failed_claim)

    async def server(socket):
        await identify_client(socket, ready)
        await socket.wait_closed()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            with pytest.raises(RuntimeError, match="synthetic sender failure"):
                await asyncio.wait_for(
                    serve_connection(
                        socket,
                        engine=engine,
                        allow_qq=USER,
                        web_url="http://testserver",
                        chat_enabled=True,
                    ),
                    2,
                )


@pytest.mark.asyncio
async def test_normal_uninitialized_chat_gets_throttled_guide_and_is_not_replayed_after_setup(
    engine, monkeypatch
):
    from lisa import gateway

    ready, delivered, drained = signal_identified(monkeypatch), asyncio.Event(), asyncio.Event()
    record = gateway.record_delivery
    service = gateway.handle_service_message
    replies = []

    def record_and_signal(*args):
        record(*args)
        delivered.set()

    monkeypatch.setattr(gateway, "record_delivery", record_and_signal)

    def service_and_signal(db, **event):
        result = service(db, **event)
        if event["message_id"] == "124":
            drained.set()
        return result

    monkeypatch.setattr(gateway, "handle_service_message", service_and_signal)

    async def server(socket):
        await identify_client(socket, ready)
        await socket.send(json.dumps(private_event(message="合成未绑定用户消息")))
        request = json.loads(await socket.recv())
        replies.append(request["params"]["message"][0]["data"]["text"])
        await send_response(socket, request, {"message_id": 456})
        await asyncio.wait_for(delivered.wait(), 2)
        await socket.send(json.dumps(private_event(message_id=124, message="另一条未绑定用户消息")))
        await asyncio.wait_for(drained.wait(), 2)
        await socket.close()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            await asyncio.wait_for(
                serve_connection(
                    socket,
                    engine=engine,
                    allow_qq=None,
                    web_url="http://testserver",
                    chat_enabled=True,
                ),
                2,
            )
    assert replies == ["请先到网页注册并绑定 QQ：http://testserver"]
    with Session(engine) as db:
        assert db.get(ServiceEvent, f"{BOT}:123").status == "sent"
        assert db.get(ServiceEvent, f"{BOT}:124").status == "ignored"
        user = Account(username="later_initialized", password_hash="synthetic")
        db.add(user)
        db.flush()
        db.add(QQBinding(user_id=user.id, qq_id=USER))
        db.add(Persona(user_id=user.id, name="合成角色", prompt="合成性格", version=1))
        db.commit()
        assert (
            gateway.ingest_message(
                db,
                bot_id=BOT,
                message_id="123",
                qq_id=USER,
                sent_at=datetime.now(UTC),
                content="合成未绑定用户消息",
            )
            is None
        )
        assert list(db.scalars(select(Task))) == []
