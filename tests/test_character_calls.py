import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_author import pending_task
from test_model_calls import model_settings as model_settings
from test_model_calls import sdk as sdk
from test_works import HEADERS, RESULT
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.author import execute_author
from lisa.characters import DEFAULT_CHARACTER_PROMPT
from lisa.model_calls import BudgetExceeded, ModelCallUnknown
from lisa.models import (
    Account,
    Chapter,
    ChapterVersion,
    Character,
    CharacterParticipation,
    Memory,
    ModelCall,
    Project,
    Task,
)

LIGHT_KNOWLEDGE = [
    {"content": "站台的灯已经亮起。", "evidence": "我看到站台亮起了灯。", "kind": "fact"}
]


def add_character(db, project_id, **changes):
    row = Character(
        **{
            "project_id": UUID(str(project_id)),
            "name": "林舟",
            "prompt": "说话简短，只给自己的反应。",
            "known_information": "我在车站等车，只知道列车晚点。",
            "current_scene": "",
            "enabled": True,
            **changes,
        }
    )
    db.add(row)
    db.commit()
    return row.id


async def forbidden(*args):
    pytest.fail("Unexpected model stage")


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_no_enabled_character_keeps_single_author_call(
    settings, engine, db, owner, project, configured
):
    if configured:
        add_character(db, project["id"], enabled=False)
    task_id = pending_task(db, owner, project)
    calls = []

    async def generate(*args):
        calls.append(args)
        return RESULT

    await execute_author(
        settings, engine, task_id, generate=generate, decide=forbidden, character_generate=forbidden
    )
    db.expire_all()
    assert len(calls) == 1 and calls[0][2] == task_id
    assert db.get(Task, task_id).status == "completed"
    assert db.scalar(select(func.count()).select_from(CharacterParticipation)) == 0


@pytest.mark.asyncio
async def test_author_can_directly_finish_without_consulting_available_character(
    settings, engine, db, owner, project
):
    character_id = add_character(db, project["id"])
    task_id = pending_task(db, owner, project)

    async def decide(*args):
        available = json.loads(args[-1][-1]["content"])["available_characters"]
        assert available == [{"id": str(character_id), "name": "林舟"}]
        return {"character_id": None, "result": RESULT}

    await execute_author(
        settings, engine, task_id, generate=forbidden, decide=decide, character_generate=forbidden
    )
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.scalar(select(func.count()).select_from(CharacterParticipation)) == 0


@pytest.mark.asyncio
async def test_character_receives_only_own_knowledge_and_suggestion_is_not_persisted(
    settings, engine, client, db, owner, project
):
    character_id = add_character(db, project["id"], current_scene="我看到站台亮起了灯。")
    add_character(db, project["id"], name="陌生人", known_information="他人角色秘密")
    work = db.get(Project, UUID(project["id"]))
    work.worldbook = "世界书秘密：城主是寄信人。"
    db.add(Memory(user_id=owner, kind="fact", content="用户私人记忆", allow_writing=True))
    db.commit()
    task_id = pending_task(db, owner, project)
    task = db.get(Task, task_id)
    task.request = {**task.request, "goal": "任务秘密：不要揭示城主身份。"}
    db.commit()
    calls = []
    suggestion = "角色临时建议：我先看一下时刻表。"

    async def decide(*args):
        calls.append(("decide", args[2], args[3]))
        return {"character_id": str(character_id), "result": None}

    async def role(*args):
        calls.append(("role", args[2], args[3]))
        instructions, messages = args[-2:]
        serialized = instructions + json.dumps(messages, ensure_ascii=False)
        for secret in ["世界书秘密", "任务秘密", "他人角色秘密", "用户私人记忆", "陌生人"]:
            assert secret not in serialized
        assert "说话简短，只给自己的反应。" in instructions
        assert json.loads(messages[0]["content"]) == {
            "name": "林舟",
            "known_information": "我在车站等车，只知道列车晚点。",
            "current_scene": "我看到站台亮起了灯。",
        }
        return {"suggestion": suggestion}

    async def generate(*args):
        calls.append(("final", args[2], args[3]))
        payload = json.loads(args[-1][-1]["content"])
        assert payload["character_suggestion"]["suggestion"] == suggestion
        assert payload["character_suggestion"]["character_id"] == str(character_id)
        return RESULT

    await execute_author(
        settings, engine, task_id, generate=generate, decide=decide, character_generate=role
    )
    db.expire_all()
    assert calls == [(stage, task_id, owner) for stage in ["decide", "role", "final"]]
    task = db.get(Task, task_id)
    assert task.status == "completed"
    assert suggestion not in json.dumps(task.input_snapshot, ensure_ascii=False)
    assert "suggestion" not in CharacterParticipation.__table__.columns
    participation = db.scalar(select(CharacterParticipation))
    assert participation.character_id == character_id
    assert participation.revision == 1 and participation.state == "completed"
    response = client.get(f"/api/tasks/{task_id}/characters")
    assert response.status_code == 200
    assert suggestion not in response.text
    assert len(response.json()["participations"]) == 1
    assert db.scalar(select(func.count()).select_from(Task)) == 1
    assert db.scalar(select(func.count()).select_from(Memory)) == 1
    # An omitted knowledge list does not implicitly archive the raw scene.
    assert db.get(Character, character_id).known_information == "我在车站等车，只知道列车晚点。"


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_account", [False, True])
async def test_author_cannot_select_character_outside_this_project(
    settings, engine, db, owner, project, foreign_account
):
    add_character(db, project["id"])
    user_id = owner
    if foreign_account:
        other = Account(username="foreign_role_owner", password_hash="synthetic")
        db.add(other)
        db.flush()
        user_id = other.id
    other_project = Project(user_id=user_id, title="另一部作品", is_current=False)
    db.add(other_project)
    db.flush()
    foreign_id = add_character(db, other_project.id)
    task_id = pending_task(db, owner, project)

    async def decide(*args):
        return {"character_id": str(foreign_id), "result": None}

    await execute_author(
        settings, engine, task_id, generate=forbidden, decide=decide, character_generate=forbidden
    )
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.error == "character_unavailable"
    assert db.scalar(select(func.count()).select_from(CharacterParticipation)) == 0
    assert db.scalar(select(func.count()).select_from(Chapter)) == 0


@pytest.mark.asyncio
async def test_cancel_character_discards_late_suggestion_without_final_author(
    settings, engine, client, db, owner, project, chapter
):
    character_id = add_character(db, project["id"], current_scene="我看到站台亮起了灯。")
    knowledge = db.get(Character, character_id).known_information
    task_id = pending_task(db, owner, project, kind="rewrite", chapter_id=chapter)
    before = client.get(f"/api/chapters/{chapter}").json()
    started, cancelled = asyncio.Event(), asyncio.Event()
    final_calls = []

    async def decide(*args):
        return {"character_id": str(character_id), "result": None}

    async def role(*args):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            return {"suggestion": "迟到的角色建议，不应保留。", "knowledge": LIGHT_KNOWLEDGE}

    async def generate(*args):
        final_calls.append(args)
        return RESULT

    running = asyncio.create_task(
        execute_author(
            settings, engine, task_id, generate=generate, decide=decide, character_generate=role
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    assert client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
    await asyncio.wait_for(running, 2)
    db.expire_all()
    assert cancelled.is_set()
    assert final_calls == []
    task = db.get(Task, task_id)
    assert task.status == "cancelled" and task.adopted_at is None
    assert client.get(f"/api/chapters/{chapter}").json() == before
    assert db.scalar(select(CharacterParticipation)).state == "cancelled"
    assert db.get(Character, character_id).known_information == knowledge
    assert "迟到的角色建议" not in json.dumps(task.input_snapshot, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["role", "final"])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [(BudgetExceeded, "budget_exhausted"), (ModelCallUnknown, "model_unknown")],
)
async def test_role_or_final_failure_keeps_formal_body_unchanged(
    settings, engine, client, db, owner, project, chapter, stage, failure, expected
):
    character_id = add_character(db, project["id"], current_scene="我看到站台亮起了灯。")
    knowledge = db.get(Character, character_id).known_information
    task_id = pending_task(db, owner, project, kind="rewrite", chapter_id=chapter)
    before = client.get(f"/api/chapters/{chapter}").json()
    calls = []

    async def decide(*args):
        return {"character_id": str(character_id), "result": None}

    async def role(*args):
        calls.append("role")
        if stage == "role":
            raise failure("synthetic failure")
        return {"suggestion": "暂时的反应", "knowledge": LIGHT_KNOWLEDGE}

    async def generate(*args):
        calls.append("final")
        raise failure("synthetic failure")

    await execute_author(
        settings, engine, task_id, generate=generate, decide=decide, character_generate=role
    )
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.error == expected and task.adopted_at is None
    assert calls == (["role"] if stage == "role" else ["role", "final"])
    assert client.get(f"/api/chapters/{chapter}").json() == before
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 1
    assert db.get(Character, character_id).known_information == knowledge
    assert db.scalar(select(CharacterParticipation)).state == (
        "failed" if stage == "role" else "completed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["novel", "diary"])
async def test_generated_character_names_are_seeded_only_with_formal_novel_adoption(
    settings, engine, db, owner, project, kind
):
    existing_id = add_character(db, project["id"], name="林舟", enabled=False)
    work = db.get(Project, UUID(project["id"]))
    work.kind = kind
    db.commit()
    task_id = pending_task(db, owner, project)

    async def generate(*args):
        assert db.scalar(select(func.count()).select_from(Character)) == 1
        return {
            **RESULT,
            "paragraphs": ["林舟和小满在车站等车。"],
            "character_names": ["林舟", "小满"],
        }

    await execute_author(settings, engine, task_id, generate=generate, decide=forbidden)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    existing = db.get(Character, existing_id)
    assert existing.prompt == "说话简短，只给自己的反应。"
    assert existing.known_information == "我在车站等车，只知道列车晚点。"
    assert existing.revision == 1 and not existing.enabled
    seeded = db.scalar(select(Character).where(Character.name == "小满"))
    if kind == "diary":
        assert seeded is None
    else:
        assert seeded.prompt == DEFAULT_CHARACTER_PROMPT
        assert seeded.known_information == "" and not seeded.enabled


@pytest.mark.asyncio
async def test_rejected_writing_never_seeds_character_configuration(
    settings, engine, db, owner, project
):
    task_id = pending_task(db, owner, project)

    async def generate(*args):
        return {
            **RESULT,
            "paragraphs": ["小满" + "字" * 601],
            "character_names": ["小满"],
        }

    await execute_author(
        settings.model_copy(update={"writing_max_chars": 600}),
        engine,
        task_id,
        generate=generate,
    )
    db.expire_all()
    assert db.get(Task, task_id).error == "writing_scope_exceeded"
    assert db.scalar(select(func.count()).select_from(Chapter)) == 0
    assert db.scalar(select(func.count()).select_from(Character)) == 0


@pytest.mark.asyncio
async def test_real_stage_wrappers_charge_same_task_without_nested_tasks(
    model_settings, sdk, engine, db, owner, project
):
    character_id = add_character(db, project["id"])
    task_id = pending_task(db, owner, project)
    results = [
        {"character_id": str(character_id), "result": None},
        {"suggestion": "我先等下一班车。"},
        RESULT,
    ]
    sdk.create.side_effect = [
        SimpleNamespace(
            status="completed",
            output_text=json.dumps(result, ensure_ascii=False),
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )
        for result in results
    ]
    await execute_author(
        model_settings.model_copy(
            update={"model_task_usd": Decimal("1"), "model_daily_usd": Decimal("2")}
        ),
        engine,
        task_id,
    )
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    rows = list(db.scalars(select(ModelCall).order_by(ModelCall.started_at)))
    assert len(rows) == 3
    assert all(row.task_id == task_id and row.user_id == owner for row in rows)
    assert all(row.state == "succeeded" and row.charged_usd > 0 for row in rows)
    assert [call.kwargs["max_output_tokens"] for call in sdk.create.call_args_list] == [
        model_settings.writing_output_tokens,
        900,
        model_settings.writing_output_tokens,
    ]
    assert db.scalar(select(func.count()).select_from(Task)) == 1


@pytest.mark.asyncio
async def test_adopted_role_knowledge_keeps_evidence_and_inference_without_duplicates(
    settings, engine, db, owner, project
):
    scene = "我看见门外停着一辆车，也许有人正在等我。"
    role_id = add_character(db, project["id"], current_scene=scene)
    other_id = add_character(db, project["id"], name="旁人", current_scene="旁人自己的场景")
    original = db.get(Character, role_id).known_information

    async def decide(*args):
        return {"character_id": str(role_id), "result": None}

    async def role(*args):
        return {
            "suggestion": "未采用建议：我已经认出了车主。",
            "knowledge": [
                {
                    "content": "门外有一辆停着的车。",
                    "evidence": "我看见门外停着一辆车",
                    "kind": "fact",
                },
                {
                    "content": "车里的人可能是在等我。",
                    "evidence": "也许有人正在等我",
                    "kind": "inference",
                },
                {"content": "司机知道信件内容。", "evidence": "司机向我展示信件", "kind": "fact"},
            ],
        }

    async def generate(*args):
        payload = json.loads(args[-1][-1]["content"])
        assert "knowledge" not in payload["character_suggestion"]
        assert "门外有一辆停着的车" not in args[-1][-1]["content"]
        assert "司机知道信件内容" not in args[-1][-1]["content"]
        return RESULT

    for _ in range(2):
        task_id = pending_task(db, owner, project)
        await execute_author(
            settings, engine, task_id, decide=decide, character_generate=role, generate=generate
        )
        db.expire_all()
        assert db.get(Task, task_id).adopted_at is not None
        saved = db.get(Character, role_id)
        assert saved.known_information.startswith(original + "\n\n")
        assert (
            saved.known_information.count(
                "事实：门外有一辆停着的车。\n场景依据：我看见门外停着一辆车"
            )
            == 1
        )
        assert (
            saved.known_information.count(
                "推测：车里的人可能是在等我。\n场景依据：也许有人正在等我"
            )
            == 1
        )
        assert saved.current_scene == scene and saved.revision == 2
        assert "认出了车主" not in saved.known_information
        assert "司机知道信件内容" not in saved.known_information
        assert "场景记录：" not in saved.known_information
        assert db.get(Character, other_id).revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["diary", "edited", "full", "unused_attempt"])
async def test_knowledge_adoption_skips_without_blocking_formal_writing(
    settings, engine, db, owner, project, reason
):
    knowledge = "字" * 5998 if reason == "full" else "我只知道列车晚点。"
    role_id = add_character(
        db, project["id"], known_information=knowledge, current_scene="我看到站台亮起了灯。"
    )
    if reason == "diary":
        db.get(Project, UUID(project["id"])).kind = "diary"
        db.commit()
    task_id = pending_task(db, owner, project)
    if reason == "unused_attempt":
        db.add(
            CharacterParticipation(
                task_id=task_id, position=1, character_id=role_id, revision=1, state="completed"
            )
        )
        db.commit()

    async def decide(*args):
        if reason == "unused_attempt":
            return {"character_id": None, "result": RESULT}
        return {"character_id": str(role_id), "result": None}

    async def role(*args):
        return {"suggestion": "我再等等。", "knowledge": LIGHT_KNOWLEDGE}

    async def generate(*args):
        if reason == "edited":
            saved = db.get(Character, role_id)
            saved.current_scene = "用户后来修改的场景。"
            saved.revision += 1
            db.commit()
        return RESULT

    await execute_author(
        settings, engine, task_id, decide=decide, character_generate=role, generate=generate
    )
    db.expire_all()
    assert db.get(Task, task_id).adopted_at is not None
    assert db.get(Character, role_id).known_information == knowledge


@pytest.mark.asyncio
async def test_knowledge_adoption_and_formal_body_roll_back_together(
    settings, engine, db, owner, project, monkeypatch
):
    from lisa.characters import adopt_character_knowledge

    role_id = add_character(db, project["id"], current_scene="我看到站台亮起了灯。")
    original = db.get(Character, role_id).known_information
    task_id = pending_task(db, owner, project)

    async def decide(*args):
        return {"character_id": str(role_id), "result": None}

    async def role(*args):
        return {"suggestion": "我再等等。", "knowledge": LIGHT_KNOWLEDGE}

    async def generate(*args):
        return RESULT

    def fail_after_adoption(*args):
        adopt_character_knowledge(*args)
        raise RuntimeError("synthetic adoption failure")

    monkeypatch.setattr("lisa.author.adopt_character_knowledge", fail_after_adoption)
    with pytest.raises(RuntimeError, match="synthetic adoption failure"):
        await execute_author(
            settings, engine, task_id, decide=decide, character_generate=role, generate=generate
        )
    db.expire_all()
    assert db.get(Task, task_id).adopted_at is None
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 0
    assert db.get(Character, role_id).known_information == original
    assert db.get(Character, role_id).revision == 1
