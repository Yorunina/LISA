"""Private QQ entry over OneBot 11, with an optional isolated test mode."""

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from lisa.chat import claim_outbound, finish_delivery, ingest_message
from lisa.db import make_engine
from lisa.models import OutboundMessage
from lisa.onboarding import handle_service_message, record_delivery
from lisa.settings import Settings
from lisa.vision import MAX_IMAGES, image_url_allowed

logger = logging.getLogger(__name__)


class OneBotFailure(Exception):
    """The gateway explicitly rejected an action."""


class OneBotUnknown(Exception):
    """No authoritative action result was received; never blindly resend."""


@dataclass(frozen=True)
class PrivateMessage:
    bot_id: str
    message_id: str
    qq_id: str
    sent_at: datetime
    text: str
    kind: str = "text"
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroupMessageEvent:
    bot_id: str
    group_id: str
    message_id: str
    qq_id: str
    name: str
    text: str
    sent_at: datetime
    kind: str = "text"


MAX_GROUP_SEGMENTS = 128
MAX_GROUP_TEXT_CHARS = 65_536
MAX_GROUP_BODY_BYTES = 262_144
GROUP_PLACEHOLDERS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "face": "[表情]",
    "mface": "[表情]",
    "at": "[提及群成员]",
    "reply": "[回复引用]",
    "forward": "[合并转发]",
    "node": "[合并转发]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
}
CQ_SEGMENT = re.compile(r"\[CQ:([A-Za-z0-9_]+)(?:,[^\]\r\n]*)?\]")


def qq_identity(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if value.isascii() and value.isdigit() and int(value) > 0 else None


def parse_private_message(
    event: dict,
    *,
    bot_id: str,
    allow_qq: str | None,
    now: datetime | None = None,
    max_age_seconds: float | None = 300,
) -> PrivateMessage | None:
    if event.get("post_type") != "message" or event.get("message_type") != "private":
        return None
    qq_id = qq_identity(event.get("user_id"))
    if (
        qq_id is None
        or (allow_qq is not None and qq_id != allow_qq)
        or qq_id == bot_id
        or qq_identity(event.get("self_id")) != bot_id
    ):
        return None
    message_id = event.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        return None
    if not str(message_id):
        return None
    timestamp = event.get("time")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    try:
        sent_at = datetime.fromtimestamp(timestamp, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    # Replayed/offline events must not mint fresh short-lived credentials.
    age = ((now or datetime.now(UTC)) - sent_at).total_seconds()
    if age < -60 or (max_age_seconds is not None and age > max_age_seconds):
        return None
    message = event.get("message")
    text = ""
    images = []
    if isinstance(message, str) and "[CQ:" not in message:
        text = message
    elif isinstance(message, list):
        parts = []
        for segment in message:
            if not isinstance(segment, dict):
                break
            data = segment.get("data")
            if not isinstance(data, dict):
                break
            if segment.get("type") == "text" and isinstance(data.get("text"), str):
                parts.append(data["text"])
            elif segment.get("type") == "image" and image_url_allowed(data.get("url")):
                if len(images) <= MAX_IMAGES:
                    images.append(data["url"])
            else:
                break
        else:
            text = "".join(parts)
            return PrivateMessage(
                bot_id,
                str(message_id),
                qq_id,
                sent_at,
                text,
                "image" if images else "text" if text else "nontext",
                tuple(images),
            )
    return PrivateMessage(
        bot_id, str(message_id), qq_id, sent_at, text, "text" if text else "nontext"
    )


def _group_identity(value) -> str | None:
    # Bound numeric parsing as these values become database keys and export fields.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        value = str(value)
    except ValueError:
        return None
    if not 1 <= len(value) <= 20:
        return None
    return qq_identity(value)


def _group_message_id(value) -> str | None:
    # OneBot message_id is signed int32, unlike positive QQ/group identities.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,9})", value):
            return None
        value = int(value)
    return str(value) if -(2**31) <= value < 2**31 else None


def parse_group_message(
    payload: dict,
    *,
    bot_id: str,
    max_age_seconds: float = 300,
    now: datetime | None = None,
) -> GroupMessageEvent | None:
    """A passive group record, with media placeholders and no image/AI processing."""
    if (
        not isinstance(payload, dict)
        or payload.get("post_type") != "message"
        or payload.get("message_type") != "group"
        or payload.get("sub_type") != "normal"
        or payload.get("anonymous") is not None
        or _group_identity(bot_id) != bot_id
        or _group_identity(payload.get("self_id")) != bot_id
    ):
        return None
    group_id = _group_identity(payload.get("group_id"))
    message_id = _group_message_id(payload.get("message_id"))
    qq_id = _group_identity(payload.get("user_id"))
    if group_id is None or message_id is None or qq_id is None or qq_id == bot_id:
        return None
    timestamp = payload.get("time")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    try:
        sent_at = datetime.fromtimestamp(timestamp, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    # The normal private entry can accept old messages, but group collection must not
    # silently import an offline history or manufacture fresh collection timestamps.
    age = ((now or datetime.now(UTC)) - sent_at).total_seconds()
    if age < -60 or age > max_age_seconds:
        return None
    sender = payload.get("sender", {})
    if not isinstance(sender, dict):
        return None
    if "user_id" in sender and _group_identity(sender["user_id"]) != qq_id:
        return None
    name = sender.get("card") or sender.get("nickname") or qq_id
    if not isinstance(name, str):
        return None
    # A display name is metadata; QQ identity is always the validated event field.
    name = " ".join(name.split())[:120] or qq_id
    message = payload.get("message")
    try:
        if len(json.dumps(message, ensure_ascii=False).encode("utf-8")) > MAX_GROUP_BODY_BYTES:
            return None
    except (ValueError, TypeError, UnicodeError):
        return None
    nontext = False
    if isinstance(message, str):

        def placeholder(match):
            nonlocal nontext
            nontext = True
            return GROUP_PLACEHOLDERS.get(match[1].lower(), "[非文本消息]")

        text = CQ_SEGMENT.sub(placeholder, message)
        if "[CQ:" in text:
            return None  # Malformed media codes cannot be mistaken for plain text.
    elif isinstance(message, list) and 0 < len(message) <= MAX_GROUP_SEGMENTS:
        parts = []
        for segment in message:
            if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                return None
            kind = segment.get("type")
            if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", kind):
                return None
            if kind == "text":
                value = segment["data"].get("text")
                if not isinstance(value, str):
                    return None
                parts.append(value)
            else:
                nontext = True
                parts.append(GROUP_PLACEHOLDERS.get(kind, "[非文本消息]"))
        text = "".join(parts)
    else:
        return None
    if not text.strip() or len(text) > MAX_GROUP_TEXT_CHARS or "\x00" in text or "\x00" in name:
        return None
    try:
        name.encode("utf-8")
        text.encode("utf-8")
    except UnicodeError:
        return None
    return GroupMessageEvent(
        bot_id,
        group_id,
        message_id,
        qq_id,
        name,
        text,
        sent_at,
        "nontext" if nontext else "text",
    )


class OneBotConnection:
    def __init__(
        self,
        websocket,
        *,
        allow_qq: str | None,
        timeout: float = 10,
        allow_old_messages: bool = False,
    ):
        self.websocket = websocket
        self.allow_qq = allow_qq
        self.timeout = timeout
        self.allow_old_messages = allow_old_messages
        self.bot_id: str | None = None
        self.last_received_at: str | None = None
        self.last_sent_at: str | None = None
        self.logged_in: bool | None = None
        self.status_observed_at: datetime | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue[PrivateMessage | GroupMessageEvent | None] = asyncio.Queue()

    async def receive(self) -> None:
        try:
            async for raw in self.websocket:
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if (
                    payload.get("post_type") == "meta_event"
                    and payload.get("meta_event_type") == "heartbeat"
                ):
                    status = payload.get("status")
                    if isinstance(status, dict):
                        online = status.get("online")
                        self.logged_in = online if isinstance(online, bool) else None
                        self.status_observed_at = datetime.now(UTC)
                echo = payload.get("echo")
                if isinstance(echo, str) and echo in self.pending:
                    future = self.pending[echo]
                    if not future.done():
                        future.set_result(payload)
                elif self.bot_id:
                    event = parse_private_message(
                        payload,
                        bot_id=self.bot_id,
                        allow_qq=self.allow_qq,
                        max_age_seconds=None if self.allow_old_messages else 300,
                    )
                    if event is None and self.allow_qq is None:
                        event = parse_group_message(payload, bot_id=self.bot_id)
                    if event:
                        self.last_received_at = datetime.now(UTC).isoformat()
                        self.events.put_nowait(event)
        except WebSocketException:
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(OneBotUnknown("Connection closed"))
            self.events.put_nowait(None)

    async def call(self, action: str, params: dict | None = None) -> dict:
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.websocket.send(
                json.dumps(
                    {"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False
                )
            )
            response = await asyncio.wait_for(future, self.timeout)
        except (TimeoutError, WebSocketException) as exc:
            raise OneBotUnknown("No confirmed action result") from exc
        finally:
            self.pending.pop(echo, None)
        if response.get("status") == "failed":
            raise OneBotFailure("Action rejected")
        if response.get("status") != "ok" or response.get("retcode") != 0:
            raise OneBotUnknown("Action result not final")
        data = response.get("data")
        if not isinstance(data, dict):
            raise OneBotUnknown("Invalid action result")
        if action == "send_private_msg":
            self.last_sent_at = datetime.now(UTC).isoformat()
        return data

    async def identify(self) -> None:
        login = await self.call("get_login_info")
        version = await self.call("get_version_info")
        bot_id = qq_identity(login.get("user_id"))
        if not bot_id or not version.get("app_name") or not version.get("app_version"):
            raise OneBotFailure("Gateway identity/version unavailable")
        self.bot_id = bot_id
        self.logged_in = True
        self.status_observed_at = datetime.now(UTC)

    async def send_private(self, qq_id: str, text: str) -> str:
        if (self.allow_qq is not None and qq_id != self.allow_qq) or qq_id == self.bot_id:
            raise ValueError("Recipient is outside the test allowlist")
        try:
            result = await self.call(
                "send_private_msg",
                {
                    "user_id": int(qq_id),
                    "message": [{"type": "text", "data": {"text": text}}],
                },
            )
            return "sent" if result.get("message_id") is not None else "unknown"
        except OneBotFailure:
            return "failed"
        except OneBotUnknown:
            return "unknown"


async def deliver_outbox(
    connection: OneBotConnection, engine, allow_qq: str | None, *, settings: Settings | None = None
) -> None:
    while True:
        with Session(engine, expire_on_commit=False) as db, db.begin():
            message = claim_outbound(db, allow_qq, settings=settings)
            if message is not None:
                from lisa.shared_retrieval import outbound_segments

                segments = outbound_segments(db, message)
        if message is None:
            await asyncio.sleep(0.5)
            continue
        status, gateway_id = "unknown", None
        try:
            if not segments:
                status = "cancelled"
            else:
                result = await connection.call(
                    "send_private_msg",
                    {
                        "user_id": int(message.qq_id),
                        "message": segments,
                    },
                )
                if result.get("message_id") is not None:
                    status, gateway_id = "sent", str(result["message_id"])
        except OneBotFailure:
            status = "failed"
        except OneBotUnknown:
            pass
        finally:
            with Session(engine) as db, db.begin():
                finish_delivery(db, message.id, status, gateway_id)
        logger.info("task=%s delivery=%s", message.task_id, status)


async def serve_connection(
    websocket,
    *,
    engine,
    allow_qq: str | None,
    web_url: str,
    chat_enabled: bool = False,
    settings: Settings | None = None,
) -> None:
    connection = OneBotConnection(websocket, allow_qq=allow_qq, allow_old_messages=chat_enabled)
    receiver = asyncio.create_task(connection.receive())
    sender = None
    pulse = None
    try:
        await connection.identify()
        from lisa.runtime_status import process_pulse

        pulse = process_pulse(
            engine,
            settings or Settings(),
            ["gateway", "qq"],
            details=lambda: {
                "connected": True,
                "logged_in": connection.logged_in
                if connection.status_observed_at
                and (datetime.now(UTC) - connection.status_observed_at).total_seconds()
                <= (settings or Settings()).operations_stale_seconds
                else None,
                "last_received_at": connection.last_received_at,
                "last_sent_at": connection.last_sent_at,
                "mode": "test" if allow_qq else "normal",
            },
        )
        await pulse.__aenter__()
        if chat_enabled:
            # A previous connection/process may have sent without recording the outcome.
            with Session(engine) as db, db.begin():
                pending = (
                    select(OutboundMessage.id)
                    .where(OutboundMessage.status == "sending")
                    .order_by(
                        OutboundMessage.user_id,
                        OutboundMessage.created_at,
                        OutboundMessage.sequence,
                    )
                )
                if allow_qq is not None:
                    pending = pending.where(OutboundMessage.qq_id == allow_qq)
                # Use the same terminal path as a live unknown acknowledgement:
                # never resend it, cancel its unsent tail, and remove raw bodies.
                for message_id in list(db.scalars(pending)):
                    finish_delivery(db, message_id, "unknown")
            sender = asyncio.create_task(
                deliver_outbox(connection, engine, allow_qq, settings=settings)
            )

            def sender_stopped(done):
                if not done.cancelled() and done.exception() is not None:
                    receiver.cancel()

            sender.add_done_callback(sender_stopped)
        logger.info("QQ gateway connected; mode=%s", "test" if allow_qq else "normal")
        while (message := await connection.events.get()) is not None:
            if isinstance(message, GroupMessageEvent):
                if allow_qq is None:
                    from lisa.group_records import ingest_group_message

                    with Session(engine) as db, db.begin():
                        ingest_group_message(db, message)
                # Group collection is a separate ingress: no binding, private-chat
                # storage, model invocation or response follows even for command text.
                continue
            command = message.text.strip()
            normal = allow_qq is None
            service_command = message.kind == "text" and command in {"绑定", "找回"}
            chat_requested = (
                chat_enabled and not service_command and (normal or command.startswith("测试对话 "))
            )
            if chat_requested:
                content = command if normal else command[len("测试对话 ") :].strip()
                if normal and message.kind == "nontext":
                    content = "[用户发送了非文本消息，当前无法读取其内容。]"
                elif message.kind == "image" and not content:
                    content = "[用户发送了图片]"
                if content:
                    with Session(engine) as db, db.begin():
                        accepted = ingest_message(
                            db,
                            bot_id=message.bot_id,
                            message_id=message.message_id,
                            qq_id=message.qq_id,
                            sent_at=message.sent_at,
                            content=content,
                            kind=message.kind,
                            image_urls=list(message.image_urls),
                        )
                    if accepted is not None:
                        continue
            if not normal and not chat_requested and command not in {"绑定", "找回", "测试引导"}:
                continue
            if (datetime.now(UTC) - message.sent_at).total_seconds() > 300:
                continue
            with Session(engine) as db, db.begin():
                event_key, reply = handle_service_message(
                    db,
                    bot_id=message.bot_id,
                    message_id=message.message_id,
                    qq_id=message.qq_id,
                    sent_at=message.sent_at,
                    text=message.text if message.kind == "text" else "",
                    web_url=web_url,
                )
            if reply is not None:
                status = await connection.send_private(message.qq_id, reply)
                with Session(engine) as db, db.begin():
                    record_delivery(db, event_key, status)
                logger.info("QQ service reply outcome: %s", status)
    finally:
        if pulse is not None:
            await pulse.__aexit__(None, None, None)
        sender_error = None
        if sender:
            sender.cancel()
            result = (await asyncio.gather(sender, return_exceptions=True))[0]
            if isinstance(result, Exception):
                sender_error = result
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        if sender_error:
            raise sender_error


async def run_gateway(settings: Settings, *, allow_qq: str | None = None) -> None:
    # An explicit recipient always selects the prefix-gated test mode.
    normal = settings.chat_enabled and allow_qq is None
    if not normal:
        allow_qq = qq_identity(allow_qq or settings.qq_test_user)
        if not allow_qq:
            raise ValueError("Set --allow-qq or LISA_QQ_TEST_USER before starting the test gateway")
    token = settings.onebot_token.get_secret_value()
    if not token:
        raise ValueError("LISA_ONEBOT_TOKEN is required")
    engine = make_engine(settings)
    try:
        while True:
            try:
                async with connect(
                    settings.onebot_ws_url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    proxy=None,
                    open_timeout=10,
                    max_size=1024 * 1024,
                ) as websocket:
                    await serve_connection(
                        websocket,
                        engine=engine,
                        allow_qq=allow_qq,
                        web_url=settings.web_url,
                        chat_enabled=settings.chat_enabled
                        if normal
                        else settings.chat_test_enabled,
                        settings=settings,
                    )
            except (OSError, WebSocketException, OneBotFailure, OneBotUnknown):
                from lisa.runtime_status import heartbeat

                await asyncio.to_thread(heartbeat, engine, "gateway", "offline", {})
                await asyncio.to_thread(heartbeat, engine, "qq", "offline", {})
                logger.warning("QQ gateway unavailable; reconnecting in 3 seconds")
            await asyncio.sleep(3)
    finally:
        engine.dispose()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=(
            "Run normal QQ private chat when LISA_CHAT_ENABLED=true; "
            "--allow-qq selects the isolated test gateway with 测试对话 prefix."
        )
    )
    parser.add_argument(
        "--allow-qq", help="Only accept this test user's QQ; defaults to LISA_QQ_TEST_USER."
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(run_gateway(Settings(), allow_qq=args.allow_qq))
    except KeyboardInterrupt:
        logger.info("QQ service-command gateway stopped")
    except SQLAlchemyError as exc:
        # SQL exceptions may include event identities in bound parameters.
        logger.error("QQ gateway database error: %s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
