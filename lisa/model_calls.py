"""Bounded Responses calls with PostgreSQL-backed development cost estimates."""

import asyncio
import json
import logging
import math
import re
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from uuid import UUID

from httpx2 import ConnectError, ConnectTimeout, PoolTimeout
from openai import APIStatusError, AsyncOpenAI, DefaultAsyncHttpxClient, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lisa.models import Account, BudgetDay, ModelCall, Task
from lisa.settings import Settings

# Conservative application default, separate from the provider model context window.
MAX_INPUT_BYTES = 64_000
MAX_INPUT_BYTES_HARD_LIMIT = 4_194_304
MAX_EMBEDDING_INPUT_BYTES = 32_000
MAX_EMBEDDING_TEXTS = 32
MAX_TASK_CALLS = 3
MAX_WRITING_CALLS = 5  # Author query + choice + character query + suggestion + final author.
MILLION = Decimal(1_000_000)
MONEY_UNIT = Decimal("0.000001")
logger = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


class ModelBusy(Exception):
    pass


class ModelCallFailed(Exception):
    pass


class ModelInputTooLarge(ModelCallFailed):
    pass


class ModelCallUnknown(Exception):
    pass


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1)

    @field_validator("reply")
    @classmethod
    def nonempty_reply(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Empty reply")
        return value.strip()


def _cost(settings: Settings, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        (
            input_tokens * settings.model_input_per_million
            + output_tokens * settings.model_output_per_million
        )
        / MILLION
    ).quantize(MONEY_UNIT, rounding=ROUND_UP)


def _reserve(
    settings: Settings,
    engine,
    task_id: UUID,
    user_id: UUID | None,
    amount: Decimal,
    *,
    configuration: dict | None = None,
    budget_exempt: bool = False,
) -> UUID:
    with Session(engine) as db, db.begin():
        # Daily rows alone cannot serialize the global in-flight limit across UTC midnight.
        db.execute(text("SELECT pg_advisory_xact_lock(7273101)"))
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        user = db.get(Account, user_id) if user_id else None
        platform = bool(
            task and task.user_id is None
            and task.kind in {"shared_analyze", "shared_index"}
            and user_id is None
        )
        if (
            task is None
            or task.user_id != user_id
            or task.status != "running"
            or (not platform and (user is None or not user.active))
        ):
            raise ModelCallFailed("Task or account unavailable")
        if budget_exempt and (configuration or {}).get("purpose") not in {
            "image_generation", "image_description"
        }:
            raise ModelCallFailed("Unsupported budget exemption")
        now = datetime.now(UTC)
        db.execute(insert(BudgetDay).values(day=now.date()).on_conflict_do_nothing())
        day = db.scalar(select(BudgetDay).where(BudgetDay.day == now.date()).with_for_update())
        calls = db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)).all()
        if any(call.state == "unknown" for call in calls):
            raise ModelCallUnknown("An earlier attempt of this task has an unknown outcome")
        budgeted_calls = [call for call in calls if not call.budget_exempt]
        task_cost = sum(
            (
                call.charged_usd if call.charged_usd is not None else call.reserved_usd
                for call in budgeted_calls
            ),
            Decimal(0),
        )
        max_calls = (
            settings.style_distillation_max_calls
            if task.kind == "style_distill"
            else (6 if task.request.get("mode") == "refine" else 1)
            if task.kind == "style_trial"
            else MAX_WRITING_CALLS
            if task.kind in {"scene", "rewrite"}
            else MAX_TASK_CALLS
        )
        if not budget_exempt and (len(budgeted_calls) >= max_calls or (
            task.kind != "style_distill" and task_cost + amount > settings.model_task_usd
        )):
            raise BudgetExceeded("Task model allowance exhausted")
        if not budget_exempt and (
            day.spent_usd + day.reserved_usd + amount > settings.model_daily_usd
        ):
            raise BudgetExceeded("Daily model allowance exhausted")
        if platform:
            maintenance = db.scalars(select(ModelCall).where(
                ModelCall.user_id.is_(None), ModelCall.budget_day == day.day
            )).all()
            used = sum((
                c.charged_usd if c.charged_usd is not None else c.reserved_usd
                for c in maintenance
            ), Decimal(0))
            if used + amount > settings.shared_daily_usd:
                raise BudgetExceeded("Daily shared maintenance allowance exhausted")
        image_call = (configuration or {}).get("purpose") == "image_generation"
        inflight = db.scalar(
            select(func.count()).select_from(ModelCall).where(
                ModelCall.state == "reserved",
                (ModelCall.configuration["purpose"].astext == "image_generation")
                if image_call else func.coalesce(ModelCall.configuration["purpose"].astext, "")
                != "image_generation",
            )
        )
        if inflight >= (1 if image_call else settings.model_concurrency):
            raise ModelBusy("Model slot occupied")
        call = ModelCall(
            task_id=task_id,
            user_id=user_id,
            budget_day=day.day,
            state="reserved",
            budget_exempt=budget_exempt,
            reserved_usd=Decimal(0) if budget_exempt else amount,
            started_at=now,
            configuration=configuration
            if configuration is not None
            else {
                "model": settings.model_name,
                "base_url": settings.model_base_url,
                "max_output_tokens": settings.model_max_output_tokens,
                "input_per_million": str(settings.model_input_per_million),
                "output_per_million": str(settings.model_output_per_million),
            },
        )
        db.add(call)
        if not budget_exempt:
            day.reserved_usd += amount
        db.flush()
        return call.id


def _finish(
    engine,
    call_id: UUID,
    *,
    state: str,
    charged: Decimal | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error: str | None = None,
) -> None:
    with Session(engine) as db, db.begin():
        db.execute(text("SELECT pg_advisory_xact_lock(7273101)"))
        call = db.get(ModelCall, call_id)
        if call is None or call.state != "reserved":
            # Deleted accounts and interrupted attempts cannot be settled twice.
            return
        day = db.scalar(select(BudgetDay).where(BudgetDay.day == call.budget_day).with_for_update())
        # No retry releases an unknown provider outcome back into available budget.
        amount = charged if charged is not None else call.reserved_usd
        if not call.budget_exempt:
            day.reserved_usd -= call.reserved_usd
            day.spent_usd += amount
        call.state, call.charged_usd = state, amount
        call.input_tokens, call.output_tokens = input_tokens, output_tokens
        call.finished_at, call.error = datetime.now(UTC), error


def _reply_request(
    settings: Settings, instructions: str, messages: list[dict], schema: type[BaseModel] = Reply
) -> dict:
    def compact_schema(value):
        if isinstance(value, dict):
            result = {
                key: compact_schema(item)
                for key, item in value.items()
                if key != "default" and (key != "title" or not isinstance(item, str))
            }
            if result.get("type") == "object":
                # Structured Outputs requires every property, even when local parsing has defaults.
                result["required"] = list(result.get("properties", {}))
                result["additionalProperties"] = False
            return result
        if isinstance(value, list):
            return [compact_schema(item) for item in value]
        return value

    request = {
        "model": settings.model_name,
        "instructions": instructions,
        "input": messages,
        "store": False,
        "max_output_tokens": settings.model_max_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema.__name__,
                "strict": True,
                "schema": compact_schema(schema.model_json_schema()),
            }
        },
    }
    if settings.model_name in {"gpt-6-astra", "gpt-6.1-sol"}:
        request["reasoning"] = {"effort": "low"}
    return request


def _request_size(request: dict) -> int:
    return len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def request_size(request: dict) -> int:
    """Return the serialized request size used by the application input guard."""
    return _request_size(request)


def trim_history(
    settings: Settings,
    instructions: str,
    messages: list[dict],
    history_count: int,
    schema: type[BaseModel] = Reply,
) -> list[dict]:
    """Drop oldest history only; keep the full instructions and current message batch."""
    for start in range(history_count + 1):
        selected = messages[start:]
        if (
            _request_size(_reply_request(settings, instructions, selected, schema))
            <= settings.model_max_input_bytes
        ):
            return selected
    raise ModelInputTooLarge("Persona and current messages exceed development input limit")


async def _provider_request(
    settings: Settings, engine, call_id: UUID, request: dict, *, endpoint: str
):
    base_url, api_key = (
        settings.embedding_connection
        if endpoint == "embeddings"
        else (settings.model_base_url, settings.model_api_key.get_secret_value())
    )
    try:
        async with (
            DefaultAsyncHttpxClient(follow_redirects=False) as http_client,
            AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,
                timeout=settings.model_timeout_seconds,
                http_client=http_client,
            ) as client,
        ):
            resource = client
            for name in endpoint.split("."):
                resource = getattr(resource, name)
            return await resource.create(**request)
    except APIStatusError as exc:
        request_id = exc.response.headers.get("x-request-id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            request_id = "unavailable"
        logger.warning(
            "call=%s http=%s request_id=%s", call_id, exc.status_code, request_id
        )
        # Rejections are definitive; a timeout or server failure may follow execution.
        rejected = 400 <= exc.status_code < 500 and exc.status_code not in {408, 499}
        _finish(
            engine,
            call_id,
            state="failed" if rejected else "unknown",
            charged=Decimal(0) if rejected else None,
            error=f"http_{exc.status_code}",
        )
        exception = ModelCallFailed if rejected else ModelCallUnknown
        raise exception("Model request rejected" if rejected else "Model outcome unknown") from None
    except (OpenAIError, TimeoutError, asyncio.CancelledError) as exc:
        cause = exc.__cause__
        # A failure before obtaining a connection cannot have reached model execution.
        not_sent = isinstance(cause, (ConnectError, ConnectTimeout, PoolTimeout))
        error = type(exc).__name__ + (f":{type(cause).__name__}" if cause else "")
        chain = []
        current = exc
        while current is not None:
            chain.append(
                {
                    "type": type(current).__name__,
                    "errno": getattr(current, "errno", None),
                    "winerror": getattr(current, "winerror", None),
                }
            )
            current = current.__cause__ or current.__context__
        logger.warning("model_call=%s transport_failure=%s", call_id, chain)
        _finish(
            engine,
            call_id,
            state="failed" if not_sent else "unknown",
            charged=Decimal(0) if not_sent else None,
            error=error[:64],
        )
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not_sent:
            raise ModelCallFailed("Model connection could not be established") from None
        raise ModelCallUnknown("Model outcome unknown") from None


async def generate_structured(
    settings: Settings,
    engine,
    task_id: UUID,
    user_id: UUID,
    instructions: str,
    messages: list[dict],
    *,
    schema: type[BaseModel] = Reply,
    output_tokens: int | None = None,
    input_byte_limit: int | None = None,
    image_description: bool = False,
) -> BaseModel:
    """One model attempt; callers must not automatically retry unknown outcomes."""
    if not settings.model_api_key.get_secret_value() or not settings.model_name:
        raise ModelCallFailed("Model API configuration unavailable")
    if output_tokens is not None:
        settings = settings.model_copy(update={"model_max_output_tokens": output_tokens})
    request = _reply_request(settings, instructions, messages, schema)
    input_bytes = _request_size(request)
    limit = settings.model_max_input_bytes if input_byte_limit is None else input_byte_limit
    if not 1 <= limit <= MAX_INPUT_BYTES_HARD_LIMIT or input_bytes > limit:
        raise ModelInputTooLarge("Model input exceeds development limit")
    # UTF-8 bytes deliberately overestimate input tokens; include protocol overhead.
    amount = _cost(settings, input_bytes + 512, settings.model_max_output_tokens)
    call_id = _reserve(
        settings,
        engine,
        task_id,
        user_id,
        amount,
        budget_exempt=image_description,
        configuration={
            **({"purpose": "image_description"} if image_description else {}),
            "model": settings.model_name,
            "base_url": settings.model_base_url,
            "max_output_tokens": settings.model_max_output_tokens,
            "input_per_million": str(settings.model_input_per_million),
            "output_per_million": str(settings.model_output_per_million),
            "request_bytes": input_bytes,
            "input_limit_bytes": limit,
        },
    )
    response = await _provider_request(settings, engine, call_id, request, endpoint="responses")

    # The SDK accepts partially shaped responses from compatible relay services.
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    usage_known = (
        type(input_tokens) is int
        and input_tokens >= 0
        and type(output_tokens) is int
        and output_tokens >= 0
    )
    charged = _cost(settings, input_tokens, output_tokens) if usage_known else amount
    if not usage_known:
        input_tokens, output_tokens = None, None
    error = None
    reply = None
    status = getattr(response, "status", None)
    if status == "completed":
        try:
            reply = schema.model_validate_json(response.output_text)
        except (ValueError, TypeError, AttributeError):
            error = "invalid_reply"
    else:
        error = "response_not_completed"
    unknown = not isinstance(status, str) or status not in {
        "completed",
        "incomplete",
        "failed",
        "cancelled",
    }
    _finish(
        engine,
        call_id,
        state="unknown" if unknown else "succeeded" if reply is not None else "failed",
        charged=amount if unknown else charged,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=error or (None if usage_known else "usage_unavailable"),
    )
    if unknown:
        raise ModelCallUnknown("Model outcome unknown")
    if reply is None:
        raise ModelCallFailed("Model response incomplete or invalid")
    return reply


async def generate_reply(settings, engine, task_id, user_id, instructions, messages) -> str:
    result = await generate_structured(settings, engine, task_id, user_id, instructions, messages)
    return result.reply


async def generate_embeddings(
    settings: Settings,
    engine,
    task_id: UUID,
    user_id: UUID,
    texts: list[str],
) -> list[list[float]]:
    """One bounded embedding attempt, charged to the same ledger as dialogue and writing."""
    base_url, api_key = settings.embedding_connection
    if (
        not settings.embedding_model
        or not base_url
        or not api_key
        or settings.embedding_input_per_million <= 0
    ):
        raise ModelCallFailed("Embedding model and input price must be configured")
    if not texts or len(texts) > settings.embedding_batch_size:
        raise ModelInputTooLarge(
            f"Embedding batch must contain 1 to {settings.embedding_batch_size} texts"
        )
    if any(not isinstance(value, str) or not value.strip() for value in texts):
        raise ModelCallFailed("Embedding texts must be nonempty strings")
    input_bytes = sum(len(value.encode("utf-8")) for value in texts)
    if input_bytes > MAX_EMBEDDING_INPUT_BYTES:
        raise ModelInputTooLarge("Embedding input exceeds development limit")

    def embedding_cost(tokens: int) -> Decimal:
        return (tokens * settings.embedding_input_per_million / MILLION).quantize(
            MONEY_UNIT,
            rounding=ROUND_UP,
        )

    amount = embedding_cost(input_bytes + 512)
    call_id = _reserve(
        settings,
        engine,
        task_id,
        user_id,
        amount,
        configuration={
            "api": "embeddings",
            "model": settings.embedding_model,
            "base_url": base_url,
            "dimensions": settings.embedding_dimensions,
            "input_per_million": str(settings.embedding_input_per_million),
            "output_per_million": "0",
        },
    )
    response = await _provider_request(
        settings,
        engine,
        call_id,
        {
            "model": settings.embedding_model,
            "input": texts,
            "dimensions": settings.embedding_dimensions,
            "encoding_format": "float",
        },
        endpoint="embeddings",
    )
    input_tokens = getattr(getattr(response, "usage", None), "prompt_tokens", None)
    usage_known = type(input_tokens) is int and input_tokens >= 0
    charged = embedding_cost(input_tokens) if usage_known else amount
    if not usage_known:
        input_tokens = None
    vectors = None
    try:
        data = response.data
        if len(data) != len(texts):
            raise ValueError("Embedding count mismatch")
        by_index = {}
        for item in data:
            if (
                type(item.index) is not int
                or not 0 <= item.index < len(texts)
                or item.index in by_index
            ):
                raise ValueError("Invalid embedding index")
            vector = item.embedding
            if (
                not isinstance(vector, list)
                or len(vector) != settings.embedding_dimensions
                or any(
                    type(value) not in {int, float} or not math.isfinite(value) for value in vector
                )
                or not any(value != 0 for value in vector)
            ):
                raise ValueError("Invalid embedding vector")
            by_index[item.index] = [float(value) for value in vector]
        vectors = [by_index[index] for index in range(len(texts))]
    except (AttributeError, TypeError, ValueError, OverflowError):
        pass
    _finish(
        engine,
        call_id,
        state="succeeded" if vectors is not None else "failed",
        charged=charged,
        input_tokens=input_tokens,
        output_tokens=0,
        error="invalid_embeddings"
        if vectors is None
        else None
        if usage_known
        else "usage_unavailable",
    )
    if vectors is None:
        raise ModelCallFailed("Embedding response invalid")
    return vectors
