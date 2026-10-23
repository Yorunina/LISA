import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from lisa import model_calls
from lisa.model_calls import BudgetExceeded, ModelCallUnknown, ModelInputTooLarge
from lisa.models import Account, ChatMessage, Memory, ModelCall, OutboundMessage, StyleCorpus, Task
from lisa.style_corpus import parse_chat, parse_writing
from lisa.style_distillation import (
    BatchAnalysis,
    Finding,
    Review,
    StyleCandidate,
    TrialOutput,
    build_plan,
    cleanup_trials,
    execute_style,
)

HEADERS = {"X-LISA-Request": "1"}
SOURCE = "临时作品中的秘密只应存在上传语料中。" * 4


@pytest.fixture
def style_owner(client, invites):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "style_reader",
            "password": "synthetic-password-2026",
            "invite_code": invites[0],
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def payload(texts, kind="writing"):
    files = []
    for index, text in enumerate(texts):
        segments = (
            parse_chat(text, index)[0] if kind == "personality" else parse_writing(text, index)
        )
        files.append({"name": f"source-{index}.txt", "text": text, "segments": segments})
    return {"files": files}


def task_with_corpus(db, user_id, texts=None, kind="writing"):
    corpus = StyleCorpus(
        user_id=user_id,
        kind=kind,
        state="processing",
        payload=payload(texts or [SOURCE], kind),
        preview={"files": [], "participants": []},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(corpus)
    db.flush()
    task = Task(
        user_id=user_id,
        kind="style_distill",
        source="web",
        status="pending",
        request={
            "kind": kind,
            "corpus_id": str(corpus.id),
            "selection": [],
            "target_speakers": ["0:p0"] if kind == "personality" else [],
        },
    )
    db.add(task)
    db.flush()
    corpus.task_id = task.id
    db.commit()
    return task.id, corpus.id


def fake_generator(calls):
    async def generate(*args, schema, **kwargs):
        content = json.loads(args[-1][0]["content"])
        calls.append((schema, content, kwargs))
        if schema is BatchAnalysis:
            ids = (
                [source["id"] for source in content["source"]]
                if "source" in content
                else [
                    source_id
                    for observation in content["observations"]
                    for finding in observation["findings"]
                    for source_id in finding["source_ids"]
                ]
            )
            return BatchAnalysis(
                findings=[
                    Finding(
                        feature="句式长短相间",
                        condition="叙事推进时",
                        source_ids=ids[:1],
                        counterexample="材料不足以判断情绪对话",
                    )
                ]
            )
        if schema is StyleCandidate:
            findings = content["observations"][0]["findings"]
            return StyleCandidate(
                prompt="叙述以具体动作推进，重要转折放慢节奏。",
                summary="动作驱动的叙述。",
                findings=findings,
            )
        if schema is TrialOutput:
            return TrialOutput(text="他在窗边放下一封新信。")
        return Review(observations=["节奏接近所观察的范围，其他语境尚不确定。"])

    return generate


def upload(client, *, text=SOURCE, kind="writing"):
    response = client.post(
        "/api/style/corpora",
        json={
            "kind": kind,
            "files": [
                {"name": "source.txt", "content_base64": base64.b64encode(text.encode()).decode()}
            ],
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_plan_covers_long_file_end_and_explicit_selected_scope(settings):
    long_text = "第一章 起\n" + "起始场景。\n" * 4000 + "第二章 末\n" + "末尾情节。\n" * 4000
    source = payload([long_text])
    plan = build_plan(source, "writing", [], [], settings)
    pieces = [unit["text"] for batch in plan.batches for unit in batch]
    assert "".join(pieces) == long_text
    assert plan.coverage["analyzed_chars"] == len(long_text)
    assert plan.holdout is None
    selected = [source["files"][0]["segments"][-1]["id"]]
    part = build_plan(source, "writing", [], selected, settings)
    assert part.coverage["selected_segments"] == 1
    assert part.coverage["selected_chars"] < len(long_text)


def test_plan_refuses_unaffordable_size_before_any_calls(settings):
    config = settings.model_copy(
        update={"style_distillation_max_calls": 3, "style_distillation_chunk_bytes": 4000}
    )
    with pytest.raises(HTTPException, match="422") as exc:
        build_plan(payload(["完整长篇。" * 10000]), "writing", [], [], config)
    assert "未开始分析" in exc.value.detail


def test_chat_target_required_and_other_speakers_retained(settings):
    text = (
        "未识别的导出说明\n2026-10-23 10:00:00 甲(123456)\n你怎么看？\n"
        "2026-10-23 10:00:01 乙(654321)\n我想一想。\n"
    )
    source = payload([text], "personality")
    with pytest.raises(HTTPException):
        build_plan(source, "personality", [], [], settings)
    plan = build_plan(source, "personality", ["0:p1"], [], settings)
    assert [s["speaker"] for s in plan.batches[0]] == ["0:p0", "0:p1"]
    assert plan.coverage["total_chars"] > plan.coverage["parsed_chars"]
    assert any("未能归属" in item for item in plan.limitations)


def test_duplicate_exports_never_become_independent_holdout(settings):
    source = payload([SOURCE, SOURCE, SOURCE])
    plan = build_plan(source, "writing", [], [], settings)
    assert plan.holdout is None
    assert any("重复" in item for item in plan.limitations)


@pytest.mark.asyncio
async def test_success_cleans_sources_without_application_or_context_side_effects(
    settings,
    engine,
    db,
    style_owner,
):
    user = db.get(Account, style_owner)
    before = (user.last_active_at, user.memory_revision)
    task_id, corpus_id = task_with_corpus(db, style_owner)
    calls = []
    await execute_style(settings, engine, task_id, generate=fake_generator(calls))
    await execute_style(settings, engine, task_id, generate=fake_generator(calls))
    db.expire_all()
    task, corpus = db.get(Task, task_id), db.get(StyleCorpus, corpus_id)
    assert task.status == "completed" and len(calls) == 2
    assert corpus.state == "cleaned" and corpus.payload == {}
    assert SOURCE not in json.dumps(task.result, ensure_ascii=False)
    assert task.input_snapshot is None and task.adopted_at is None
    assert task.result["report"]["validation"]["holdout"]["status"] == "not_executed"
    assert task.result["report"]["source_state"] == "cleaned"
    db.refresh(user)
    assert (user.last_active_at, user.memory_revision) == before
    for model in [ChatMessage, Memory, OutboundMessage]:
        assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_long_analysis_reduces_observations_with_finite_full_coverage(
    settings,
    engine,
    db,
    style_owner,
):
    text = "第一章 开始\n" + "长篇正文场景。\n" * 7000 + "第二章 最后\n末尾需要被覆盖。"
    task_id, _ = task_with_corpus(db, style_owner, [text])
    plan = build_plan(payload([text]), "writing", [], [], settings)
    calls = []
    await execute_style(settings, engine, task_id, generate=fake_generator(calls))
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed", task.error
    assert len(calls) == plan.calls
    assert any("observations" in content for schema, content, _ in calls if schema is BatchAnalysis)
    analyzed = [
        source["text"]
        for schema, content, _ in calls
        if schema is BatchAnalysis
        for source in content.get("source", [])
    ]
    assert "".join(analyzed) == text
    assert task.result["report"]["coverage"]["analyzed_chars"] == len(text)


@pytest.mark.asyncio
async def test_holdout_generation_cannot_see_answers(settings, engine, db, style_owner):
    texts = ["首部作品的材料甲。" * 8, "开发材料乙的内容。" * 8, "绝密留出答案丙的内容。" * 8]
    task_id, _ = task_with_corpus(db, style_owner, texts)
    calls = []
    await execute_style(settings, engine, task_id, generate=fake_generator(calls))
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed"
    assert len(calls) == 5
    synthesis = next(content for schema, content, _ in calls if schema is StyleCandidate)
    generator = next(content for schema, content, _ in calls if schema is TrialOutput)
    assert "绝密留出" not in json.dumps(synthesis, ensure_ascii=False)
    assert "绝密留出" not in json.dumps(generator, ensure_ascii=False)
    assert task.result["report"]["validation"]["holdout"]["status"] == (
        "generated_and_model_reviewed"
    )


@pytest.mark.asyncio
async def test_chat_holdout_checks_two_turns_without_actual_target_replies(
    settings,
    engine,
    db,
    style_owner,
):
    texts = [
        "2026-10-23 10:00:00 甲\n问题甲\n2026-10-23 10:00:01 乙\n回复甲\n",
        "2026-10-23 11:00:00 甲\n问题乙\n2026-10-23 11:00:01 乙\n回复乙\n",
        "2026-10-23 12:00:00 甲\n留出问题\n2026-10-23 12:00:01 乙\n保密回复一\n"
        "2026-10-23 12:00:02 甲\n继续话题\n2026-10-23 12:00:03 乙\n保密回复二\n",
    ]
    task_id, _ = task_with_corpus(db, style_owner, texts, "personality")
    task = db.get(Task, task_id)
    task.request = {**task.request, "target_speakers": ["0:p1", "1:p1", "2:p1"]}
    db.commit()
    calls = []
    await execute_style(settings, engine, task_id, generate=fake_generator(calls))
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed"
    probes = [content for schema, content, _ in calls if schema is TrialOutput]
    assert len(probes) == 2
    assert "保密回复" not in json.dumps(probes, ensure_ascii=False)
    assert probes[1]["previous_generated_turns"] == ["他在窗边放下一封新信。"]
    assert task.result["report"]["validation"]["holdout"]["generated_turns"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code",
    [
        (BudgetExceeded, "budget_exhausted"),
        (ModelCallUnknown, "model_unknown"),
        (ModelInputTooLarge, "input_too_large"),
    ],
)
async def test_terminal_failure_cleans_without_replaying(
    settings,
    engine,
    db,
    style_owner,
    error,
    code,
):
    task_id, corpus_id = task_with_corpus(db, style_owner)
    count = 0

    async def fail(*args, **kwargs):
        nonlocal count
        count += 1
        raise error("do not retain the material in this diagnostic")

    await execute_style(settings, engine, task_id, generate=fail)
    await execute_style(settings, engine, task_id, generate=fail)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.error == code and task.result is None
    assert count == 1
    assert db.get(StyleCorpus, corpus_id).payload == {}


@pytest.mark.asyncio
async def test_copied_source_is_refused_instead_of_saved(settings, engine, db, style_owner):
    task_id, corpus_id = task_with_corpus(db, style_owner)

    async def copied(*args, **kwargs):
        return BatchAnalysis(
            findings=[
                Finding(feature=SOURCE, condition="任何情境", source_ids=["0:0"], counterexample="")
            ]
        )

    await execute_style(settings, engine, task_id, generate=copied)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.error == "source_copy_detected"
    assert task.result is None and db.get(StyleCorpus, corpus_id).payload == {}


@pytest.mark.asyncio
async def test_short_chat_quote_is_not_kept_as_evidence(settings, engine, db, style_owner):
    text = "2026-10-23 10:00:00 甲\n别急我想想办法。\n"
    task_id, corpus_id = task_with_corpus(db, style_owner, [text], "personality")

    async def copied(*args, **kwargs):
        return BatchAnalysis(
            findings=[
                Finding(
                    feature="会说“别急我想想办法”",
                    condition="朋友求助时",
                    source_ids=["0:0"],
                    counterexample="",
                )
            ]
        )

    await execute_style(settings, engine, task_id, generate=copied)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.error == "source_copy_detected"
    assert task.result is None and db.get(StyleCorpus, corpus_id).payload == {}


@pytest.mark.asyncio
async def test_cancellation_aborts_inflight_and_discards_late_success(
    settings,
    engine,
    db,
    client,
    style_owner,
):
    task_id, corpus_id = task_with_corpus(db, style_owner)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            return BatchAnalysis()

    running = asyncio.create_task(execute_style(settings, engine, task_id, generate=delayed))
    await asyncio.wait_for(entered.wait(), 3)
    response = client.post(f"/api/style/tasks/{task_id}/cancel", headers=HEADERS)
    assert response.status_code == 200
    await asyncio.wait_for(running, 3)
    assert cancelled.is_set()
    db.expire_all()
    assert db.get(Task, task_id).status == "cancelled"
    assert db.get(Task, task_id).result is None
    assert db.get(StyleCorpus, corpus_id).payload == {}


@pytest.mark.asyncio
async def test_account_deleted_during_analysis_cannot_be_recreated(
    settings, engine, db, style_owner
):
    task_id, corpus_id = task_with_corpus(db, style_owner)

    async def deleted(*args, **kwargs):
        with Session(engine) as session, session.begin():
            session.execute(delete(Account).where(Account.id == style_owner))
        return BatchAnalysis()

    await execute_style(settings, engine, task_id, generate=deleted)
    db.expire_all()
    assert db.get(Task, task_id) is None
    assert db.get(StyleCorpus, corpus_id) is None


@pytest.mark.asyncio
async def test_edited_unsaved_trial_is_isolated_and_erases_input(
    settings,
    engine,
    client,
    db,
    style_owner,
):
    response = client.post(
        "/api/style/trials",
        json={
            "kind": "writing",
            "prompt": "这是未保存的临时笔法。",
            "input": "写一段邮局短场景。",
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    task_id = UUID(response.json()["id"])
    seen = []

    async def generate(*args, **kwargs):
        seen.append(args)
        return TrialOutput(text="邮戳落在信封的一角。")

    await execute_style(settings, engine, task_id, generate=generate)
    task = db.get(Task, task_id)
    assert task.request == {"kind": "writing"}
    assert task.result == {"text": "邮戳落在信封的一角。", "isolated": True}
    assert "未保存的临时笔法" in seen[0][-2]
    assert client.get(f"/api/style/tasks/{task_id}").json()["result"]["isolated"]
    task.created_at = datetime.now(UTC) - timedelta(days=2)
    db.commit()
    assert cleanup_trials(db) == 1
    db.commit()
    assert task.result is None


def test_task_api_works_before_persona_initialization_and_checks_ownership(
    client,
    db,
    style_owner,
):
    corpus = upload(client)
    response = client.post("/api/style/tasks", json={"corpus_id": corpus["id"]}, headers=HEADERS)
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["planned_calls"] == 2
    assert task["cleanup_state"] == "processing"
    assert "text" not in db.get(Task, UUID(task["id"])).request
    assert client.get("/api/style/tasks").json()[0]["id"] == task["id"]
    other = Account(username="other_style", password_hash="synthetic-hash")
    db.add(other)
    db.flush()
    private = Task(user_id=other.id, kind="style_trial", source="web", status="completed")
    db.add(private)
    db.commit()
    assert client.get(f"/api/style/tasks/{private.id}").status_code == 404
    assert client.post(f"/api/style/tasks/{private.id}/cancel", headers=HEADERS).status_code == 404
    assert client.post(f"/api/style/tasks/{task['id']}/cancel", headers=HEADERS).json()[
        "status"
    ] == ("cancelled")
    assert db.get(StyleCorpus, UUID(corpus["id"])).payload == {}


@pytest.mark.asyncio
async def test_distillation_ledger_and_trial_limits(settings, engine, db, style_owner, monkeypatch):
    config = settings.model_copy(
        update={
            "model_api_key": SecretStr("synthetic"),
            "model_daily_usd": Decimal("100"),
            "model_task_usd": Decimal("100"),
            "style_distillation_max_calls": 4,
        }
    )
    task = Task(user_id=style_owner, kind="style_distill", source="web", status="running")
    db.add(task)
    db.commit()

    async def provider(*args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            status="completed",
            output_text='{"text":"试用输出"}',
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )

    monkeypatch.setattr(model_calls, "_provider_request", provider)
    for _ in range(4):
        await model_calls.generate_structured(
            config,
            engine,
            task.id,
            style_owner,
            "隔离试用",
            [{"role": "user", "content": "写一个片段"}],
            schema=TrialOutput,
        )
    with pytest.raises(BudgetExceeded):
        await model_calls.generate_structured(
            config,
            engine,
            task.id,
            style_owner,
            "隔离试用",
            [{"role": "user", "content": "写一个片段"}],
            schema=TrialOutput,
        )
    calls = db.scalars(select(ModelCall).where(ModelCall.task_id == task.id)).all()
    assert len(calls) == 4
    assert all(
        "写一个片段" not in json.dumps(call.configuration, ensure_ascii=False) for call in calls
    )
    trial = Task(user_id=style_owner, kind="style_trial", source="web", status="running")
    db.add(trial)
    db.commit()
    await model_calls.generate_structured(
        config,
        engine,
        trial.id,
        style_owner,
        "隔离试用",
        [{"role": "user", "content": "写一个片段"}],
        schema=TrialOutput,
    )
    with pytest.raises(BudgetExceeded):
        await model_calls.generate_structured(
            config,
            engine,
            trial.id,
            style_owner,
            "隔离试用",
            [{"role": "user", "content": "写一个片段"}],
            schema=TrialOutput,
        )
