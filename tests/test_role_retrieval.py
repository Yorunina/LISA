import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_retrieval import index_all, memory, vectors
from test_retrieval import library as library
from test_retrieval import vector_settings as vector_settings

from lisa.character_calls import consult_character
from lisa.model_calls import EmbeddingUnavailable
from lisa.models import (
    Character,
    CharacterParticipation,
    IndexChunk,
    IndexState,
    ModelCall,
    Project,
    RetrievalReference,
    Task,
)
from lisa.recall import retrieve_context
from lisa.retrieval import Context, has_ready_context, recall


def character(db, project_id, name="小满", knowledge="我知道列车会晚点。", **changes):
    item = Character(
        **{
            "project_id": project_id,
            "name": name,
            "prompt": "只从自己的视角反应。",
            "known_information": knowledge,
            "enabled": True,
            "retrieval_config": {"enabled": True, "limit": 3, "max_chars": 1500},
            **changes,
        }
    )
    db.add(item)
    db.flush()
    return item.id


def running_task(db, owner, project_id):
    item = Task(
        user_id=owner,
        project_id=project_id,
        kind="scene",
        status="running",
        request={"goal": "作者绝密目标：幕后人物将要出场。"},
    )
    db.add(item)
    db.commit()
    return item.id


@pytest.mark.asyncio
async def test_role_query_does_not_swallow_definitive_embedding_failure(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    role_id = character(db, project)
    task_id = running_task(db, owner, project)
    db.commit()
    await index_all(engine, vector_settings)
    generate = AsyncMock()
    with pytest.raises(EmbeddingUnavailable):
        await consult_character(
            vector_settings, engine, task_id, owner, project, role_id,
            embed=AsyncMock(side_effect=EmbeddingUnavailable("settled failure")),
            generate=generate,
        )
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_filters_character_project_and_owner_before_ranking(
    db, engine, vector_settings, library
):
    owner, other, project, other_project, foreign_project = library
    own = character(db, project)
    character(db, project, "同作其他人物", "最近：同作人物秘密")
    distant = character(db, other_project, "其他作品人物", "最近：其他作品秘密")
    foreign = character(db, foreign_project, "其他用户人物", "最近：其他用户秘密")
    character(db, project, "停用人物", "最近：停用人物秘密", enabled=False)
    memory(db, owner, "最近：用户私人记忆")
    db.get(Project, project).worldbook = "最近：全局世界书秘密"
    db.commit()
    await index_all(engine, vector_settings)
    context = recall(
        db, vector_settings, owner, project, "role", [1, 0, 0], limit=1, character_id=own
    )
    assert "我知道列车会晚点。" in context.text
    assert "最近" not in context.text
    assert context.memory_ids == []
    assert [item["source_id"] for item in context.references] == [str(own)]
    assert context.references[0]["source_kind"] == "character"
    assert has_ready_context(db, vector_settings, owner, project, "role", character_id=own)
    for user_id, project_id, role_id in [
        (owner, project, distant),
        (owner, project, foreign),
        (owner, foreign_project, foreign),
        (other, project, own),
        (owner, None, own),
        (owner, project, None),
    ]:
        assert not has_ready_context(
            db, vector_settings, user_id, project_id, "role", character_id=role_id
        )
        assert (
            recall(
                db, vector_settings, user_id, project_id, "role", [1, 0, 0], character_id=role_id
            )
            == Context()
        )


@pytest.mark.asyncio
async def test_role_knowledge_never_enters_chat_writing_or_unknown_purpose(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    role_id = character(db, project, knowledge="角色独有秘密")
    db.commit()
    await index_all(engine, vector_settings)
    for purpose in ["chat", "writing", "unknown"]:
        assert not has_ready_context(
            db, vector_settings, owner, project, purpose, character_id=role_id
        )
        assert (
            recall(db, vector_settings, owner, project, purpose, [1, 0, 0], character_id=role_id)
            == Context()
        )
    memory(db, owner, "普通聊天与创作记忆", allow_writing=True)
    db.commit()
    await index_all(engine, vector_settings)
    assert recall(db, vector_settings, owner, project, "unknown", [1, 0, 0]) == Context()
    for purpose in ["chat", "writing"]:
        context = recall(db, vector_settings, owner, project, purpose, [1, 0, 0])
        assert "普通聊天与创作记忆" in context.text
        assert "角色独有秘密" not in context.text


@pytest.mark.asyncio
async def test_role_indexes_only_enabled_knowledge_and_reuses_existing_vectors(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    role_id = character(
        db, project, prompt="不可索引的角色指令", current_scene="不可索引的当前场景"
    )
    disabled = character(db, project, "停用人物", "不应索引的停用知识", enabled=False)
    db.commit()
    texts = []

    async def embed(*args):
        texts.extend(args[-1])
        return await vectors(*args)

    await index_all(engine, vector_settings, embed)
    assert texts == ["我知道列车会晚点。"]
    before = db.scalar(select(func.count()).select_from(IndexChunk))
    assert before == 1
    assert db.scalar(select(IndexState).where(IndexState.source_id == disabled)) is None
    assert await index_all(engine, vector_settings, embed) == 0
    assert recall(
        db, vector_settings, owner, project, "role", [1, 0, 0], character_id=role_id
    ).text
    assert db.scalar(select(func.count()).select_from(IndexChunk)) == before
    assert texts == ["我知道列车会晚点。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "disable", "delete"])
async def test_role_source_changes_immediately_exclude_old_index(
    db, engine, vector_settings, library, change
):
    owner, _, project, _, _ = library
    role_id = character(db, project)
    db.commit()
    await index_all(engine, vector_settings)
    role = db.get(Character, role_id)
    if change == "edit":
        role.known_information, role.revision = "我刚知道末班车取消了。", 2
    elif change == "disable":
        role.enabled = False
    else:
        db.delete(role)
    db.commit()
    assert not has_ready_context(db, vector_settings, owner, project, "role", character_id=role_id)
    assert (
        recall(db, vector_settings, owner, project, "role", [1, 0, 0], character_id=role_id)
        == Context()
    )
    if change == "edit":
        await index_all(engine, vector_settings)
        context = recall(
            db, vector_settings, owner, project, "role", [1, 0, 0], character_id=role_id
        )
        assert "末班车取消" in context.text and "列车会晚点" not in context.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["config_off", "role_off", "delete", "scope", "budget"])
async def test_role_query_rechecks_configuration_and_permissions_after_embedding(
    db, engine, vector_settings, library, change
):
    owner, _, project, other_project, _ = library
    role_id = character(db, project, knowledge="这是角色知道的长段落。" * 90)
    db.commit()
    await index_all(engine, vector_settings)
    task_id = running_task(db, owner, project)
    calls = []

    async def embed(*args):
        calls.append(args[-1])
        with Session(engine) as writer, writer.begin():
            role = writer.get(Character, role_id)
            if change == "config_off":
                role.retrieval_config = {"enabled": False, "limit": 3, "max_chars": 1500}
            elif change == "role_off":
                role.enabled = False
            elif change == "delete":
                writer.delete(role)
            elif change == "scope":
                role.project_id = other_project
            else:
                role.retrieval_config = {"enabled": True, "limit": 1, "max_chars": 300}
        return [[1, 0, 0]]

    context = await retrieve_context(
        vector_settings,
        engine,
        task_id,
        owner,
        project,
        "role",
        "车站场景",
        character_id=role_id,
        embed=embed,
    )
    assert calls == [["车站场景"]]
    if change == "budget":
        assert context.text and len(context.text) <= 300
        assert len(context.references) == 1
    else:
        assert context == Context()


@pytest.mark.asyncio
async def test_disabled_role_retrieval_skips_query_embedding_and_charges(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    role_id = character(db, project, retrieval_config={})
    db.commit()
    await index_all(engine, vector_settings)
    task_id = running_task(db, owner, project)

    async def forbidden(*args):
        pytest.fail("Disabled role retrieval must not call an embedding model")

    assert (
        await retrieve_context(
            vector_settings,
            engine,
            task_id,
            owner,
            project,
            "role",
            "当前场景",
            character_id=role_id,
            embed=forbidden,
        )
        == Context()
    )
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scene", ["我正站在车站等末班车。", ""])
async def test_consult_character_query_uses_only_own_scene_or_name(
    db, engine, vector_settings, library, monkeypatch, scene
):
    owner, _, project, _, _ = library
    role_id = character(db, project, current_scene=scene)
    character(db, project, "他人角色", "他人角色绝密知识")
    db.get(Project, project).worldbook = "世界书绝密内容"
    memory(db, owner, "用户私人绝密记忆")
    db.commit()
    await index_all(engine, vector_settings)
    task_id = running_task(db, owner, project)
    queries = []

    async def embed(*args):
        queries.extend(args[-1])
        return [[1, 0, 0]]

    async def generate(*args):
        instructions, messages = args[-2:]
        text = instructions + json.dumps(messages, ensure_ascii=False)
        for forbidden in ["作者绝密目标", "世界书绝密内容", "用户私人绝密记忆", "他人角色绝密知识"]:
            assert forbidden not in text
        assert "列车会晚点" in text
        return {"suggestion": "我再看一眼时刻表。"}

    monkeypatch.setattr("lisa.recall.generate_embeddings", embed)
    result = await consult_character(
        vector_settings, engine, task_id, owner, project, role_id, generate=generate
    )
    assert queries == [scene or "小满"]
    assert result["suggestion"] == "我再看一眼时刻表。"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_character_without_vectors_keeps_explicit_knowledge_fallback_bounded(
    db, engine, vector_settings, library, enabled
):
    owner, _, project, _, _ = library
    knowledge = "已明确给角色的知识。" * 120
    role_id = character(
        db,
        project,
        knowledge=knowledge,
        retrieval_config={"enabled": enabled, "limit": 3, "max_chars": 300},
    )
    db.commit()
    task_id = running_task(db, owner, project)

    async def generate(*args):
        payload = json.loads(args[-1][0]["content"])
        assert payload["known_information"] == (knowledge[:300] if enabled else knowledge)
        return {"suggestion": "我只按自己知道的事回答。"}

    assert await consult_character(
        vector_settings, engine, task_id, owner, project, role_id, generate=generate
    )
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retrieval_enabled", [True, False])
async def test_consult_uses_latest_role_after_query_edit_without_version_conflict(
    db, engine, vector_settings, library, retrieval_enabled
):
    owner, _, project, _, _ = library
    role_id = character(db, project, current_scene="旧场景：站在候车厅。")
    db.commit()
    await index_all(engine, vector_settings)
    task_id = running_task(db, owner, project)
    latest_knowledge = "新知识：末班车已取消，我正在寻找回家的路。" * 40
    stages = []

    async def embed(*args):
        stages.append("embedding")
        assert args[-1] == ["旧场景：站在候车厅。"]
        with Session(engine) as writer, writer.begin():
            role = writer.get(Character, role_id)
            role.name = "更新后的小满"
            role.prompt = "新要求：直说自己现在知道的事。"
            role.definition = {"identity": "车站归客", "personality": "直率"}
            role.current_scene = "新场景：站在车站门外。"
            role.known_information = latest_knowledge
            role.retrieval_config = {
                "enabled": retrieval_enabled,
                "limit": 1,
                "max_chars": 300,
            }
            role.revision += 1
        return [[1, 0, 0]]

    async def generate(*args):
        stages.append("generation")
        instructions, messages = args[-2:]
        assert "新要求：直说自己现在知道的事。" in instructions
        payload = json.loads(messages[0]["content"])
        assert payload == {
            "name": "更新后的小满",
            "definition": {
                "identity": "车站归客",
                "personality": "直率",
                "motivation": "",
                "relationships": "",
                "voice": "",
            },
            "known_information": latest_knowledge[:300] if retrieval_enabled else latest_knowledge,
            "current_scene": "新场景：站在车站门外。",
        }
        assert "列车会晚点" not in messages[0]["content"]
        return {"suggestion": "我得换一种方式回家。"}

    result = await consult_character(
        vector_settings,
        engine,
        task_id,
        owner,
        project,
        role_id,
        embed=embed,
        generate=generate,
    )
    assert stages == ["embedding", "generation"]
    assert result["name"] == "更新后的小满"
    db.expire_all()
    assert db.get(Task, task_id).status == "running"
    assert db.get(Task, task_id).error is None
    participation = db.get(CharacterParticipation, (task_id, 1))
    assert participation.revision == 2 and participation.state == "completed"
    assert db.scalar(select(func.count()).select_from(RetrievalReference)) == 0


@pytest.mark.asyncio
async def test_cancelled_task_during_role_query_does_not_enter_generation(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    role_id = character(db, project)
    db.commit()
    await index_all(engine, vector_settings)
    task_id = running_task(db, owner, project)
    queried = []

    async def embed(*args):
        queried.extend(args[-1])
        with Session(engine) as writer, writer.begin():
            writer.get(Task, task_id).status = "cancelled"
        return [[1, 0, 0]]

    async def generate(*args):
        pytest.fail("A late embedding result must not start generation after cancellation")

    assert (
        await consult_character(
            vector_settings,
            engine,
            task_id,
            owner,
            project,
            role_id,
            embed=embed,
            generate=generate,
        )
        is None
    )
    assert queried == ["小满"]
    db.expire_all()
    assert db.get(Task, task_id).status == "cancelled"
    assert db.get(CharacterParticipation, (task_id, 1)).state == "cancelled"
    assert db.scalar(select(func.count()).select_from(RetrievalReference)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
