import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from lisa.gateway import OneBotConnection, parse_private_message, run_gateway, serve_connection
from lisa.models import (
    Account,
    ChatMessage,
    OutboundMessage,
    Persona,
    QQBinding,
    ServiceEvent,
    Task,
)

BOT = "10001"
USER = "20002"


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
    assert commands == ["绑定", "找回"]
    with Session(engine) as db:
        rows = list(db.scalars(select(ChatMessage).order_by(ChatMessage.event_key)))
        assert [(db.get(QQBinding, row.user_id).qq_id, row.content, row.kind) for row in rows] == [
            (USER, "普通私聊", "text"),
            ("30003", "测试对话 保留这段原文", "text"),
            (USER, "[用户发送了非文本消息，当前无法读取其内容。]", "nontext"),
        ]
        assert rows[0].task_id == rows[2].task_id != rows[1].task_id


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
