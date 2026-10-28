import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from procrastinate.exceptions import JobRetry
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.model_calls import BudgetExceeded, ModelBusy, ModelCallFailed, ModelCallUnknown
from lisa.models import (
    Account,
    Chapter,
    ChapterVersion,
    ChatMessage,
    IndexChunk,
    IndexState,
    Memory,
    Project,
    RetrievalReference,
    Task,
)
from lisa.retrieval import (
    Context,
    Source,
    _assemble_context,
    embedding_space,
    ensure_index_tasks,
    execute_index,
    has_ready_context,
    load_task_references,
    recall,
    record_references,
)
from lisa.works import paragraph_document


@pytest.fixture
def vector_settings(settings):
    return settings.model_copy(
        update={
            "embedding_model": "synthetic-embedding",
            "embedding_dimensions": 3,
            "embedding_input_per_million": Decimal("1"),
            "model_api_key": SecretStr("synthetic-key"),
        }
    )


@pytest.fixture
def library(db):
    owner = Account(username="retrieval_owner", password_hash="synthetic")
    other = Account(username="retrieval_other", password_hash="synthetic")
    db.add_all([owner, other])
    db.flush()
    project = Project(user_id=owner.id, title="合成小说", kind="novel")
    other_project = Project(user_id=owner.id, title="另一部作品", is_current=False)
    foreign_project = Project(user_id=other.id, title="其他用户作品")
    db.add_all([project, other_project, foreign_project])
    db.commit()
    return owner.id, other.id, project.id, other_project.id, foreign_project.id


async def vectors(settings, engine, task_id, user_id, texts):
    return [[1.0, 0.0, 0.0] if "最近" in text else [0.8, 0.2, 0.0] for text in texts]


async def index_all(engine, settings, embed=vectors):
    count = 0
    for _ in range(30):
        with Session(engine) as db, db.begin():
            ids = [task.id for task in ensure_index_tasks(db, settings)]
        if not ids:
            return count
        for task_id in ids:
            await execute_index(settings, engine, task_id, embed=embed)
            count += 1
    raise AssertionError("Synthetic indexing failed to finish")


def memory(db, owner, content="可以召回的有效事实", **kwargs):
    item = Memory(user_id=owner, content=content, kind="fact", **kwargs)
    db.add(item)
    db.flush()
    return item


def test_memory_hit_keeps_full_condition_basis_and_deduplicates_slices():
    body = "过去可以借用。" + "背景。" * 180 + "现在不再借出；仅在周末例外。\n推测依据：合成依据。"
    source = Source(
        "memory", uuid4(), uuid4(), 2, body,
        {"type": "inference", "scope": "relationship", "allow_writing": False},
        [("memory", 0, len(body))],
    )
    candidates = [(source, 0, 500, 0, "synthetic"), (source, 500, len(body), 1, "synthetic")]
    result = _assemble_context(candidates, 1500, 5)
    entries = [json.loads(line) for line in result.text.splitlines()[2:]]
    assert len(entries) == 1 and entries[0]["text"] == body
    assert entries[0]["type"] == "inference"
    assert result.memory_ids == [source.id]
    assert len(result.references) == 1
    assert result.references[0]["start"] == 0
    assert result.references[0]["end"] == len(body)


def test_whole_memory_packing_skips_long_items_and_honors_exact_serialized_boundary():
    long = Source("memory", uuid4(), uuid4(), 1, "长条" * 1500, {}, [("memory", 0, 3000)])
    short = Source("memory", uuid4(), long.user_id, 1, "现在不再外借。", {}, [("memory", 0, 7)])
    candidates = [(long, 0, 700, 0, "synthetic"), (short, 0, 7, 0, "synthetic")]
    result = _assemble_context(candidates, 500, 5)
    assert "长条" not in result.text and short.text in result.text
    assert result.memory_ids == [short.id]
    exact = len(result.text)
    assert _assemble_context(candidates, exact, 5) == result
    assert _assemble_context(candidates, exact - 1, 5) == Context()
    assert _assemble_context(candidates[:1], 500, 5) == Context()
    # The existing chapter/role fragment policy is outside B14's memory-only fix.
    chapter = Source("chapter", uuid4(), long.user_id, 1, long.text, {}, [("chapter", 0, 3000)])
    partial = _assemble_context([(chapter, 0, 700, 0, "synthetic")], 500, 5)
    assert partial.text and 0 < partial.references[0]["end"] < 700


@pytest.mark.asyncio
async def test_filter_before_rank_respects_owner_project_usage_and_active(
    db, engine, vector_settings, library
):
    owner, other, project, other_project, foreign = library
    target = memory(db, owner)
    target_id = target.id
    memory(db, owner, "最近：仅供创作", allow_chat=False, allow_writing=True)
    memory(db, owner, "最近：另一项目", project_id=other_project)
    memory(db, other, "最近：其他用户")
    memory(db, owner, "最近：已经删除", active=False)
    db.commit()
    await index_all(engine, vector_settings)
    context = recall(db, vector_settings, owner, project, "chat", [1, 0, 0], limit=1)
    assert context.memory_ids == [target_id]
    assert "有效事实" in context.text and "最近" not in context.text
    assert has_ready_context(db, vector_settings, owner, project, "chat")
    assert not has_ready_context(db, vector_settings, owner, foreign, "chat")
    assert recall(db, vector_settings, owner, foreign, "chat", [1, 0, 0]) == Context()
    assert "仅供创作" in recall(db, vector_settings, owner, project, "writing", [1, 0, 0]).text


@pytest.mark.asyncio
async def test_live_memory_correction_excludes_old_slice_but_not_corrected_source(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    source = ChatMessage(
        user_id=owner, role="user", content="旧来源事实", sent_at=datetime.now(UTC)
    )
    db.add(source)
    db.flush()
    item = memory(db, owner, "喜欢旧事物", source_message_id=source.id)
    item_id = item.id
    db.commit()
    await index_all(engine, vector_settings)
    context = recall(db, vector_settings, owner, project, "chat", [1, 0, 0])
    assert "喜欢旧事物" in context.text
    item = db.get(Memory, item_id)
    item.content, item.revision = "喜欢新的事物", 2
    source.context_excluded = True
    db.commit()
    assert recall(db, vector_settings, owner, project, "chat", [1, 0, 0]).text == ""
    await index_all(engine, vector_settings)
    assert "喜欢新的事物" in recall(db, vector_settings, owner, project, "chat", [1, 0, 0]).text
    item = db.get(Memory, item_id)
    item.allow_chat = False
    db.commit()
    assert not has_ready_context(db, vector_settings, owner, project, "chat")


@pytest.mark.asyncio
async def test_current_material_chapter_and_diary_exclusions(db, engine, vector_settings, library):
    owner, _, project_id, _, _ = library
    project = db.get(Project, project_id)
    project.worldbook = "世界书的有效文字"
    project.author_prompt = "不要索引作者指令"
    project.constraints = "不要索引固定约束"
    chapter = Chapter(
        project_id=project_id,
        title="当前章节",
        position=1,
        content=paragraph_document(["当前正文"]),
    )
    db.add(chapter)
    db.flush()
    chapter_id = chapter.id
    db.add(
        ChapterVersion(
            chapter_id=chapter.id,
            version=0,
            title="旧章节",
            content=paragraph_document(["历史内容不能召回"]),
            source="web",
        )
    )
    db.commit()
    await index_all(engine, vector_settings)
    context = recall(db, vector_settings, owner, project_id, "writing", [1, 0, 0], max_chars=4000)
    assert "有效文字" in context.text and "当前正文" in context.text
    assert (
        "作者指令" not in context.text
        and "固定约束" not in context.text
        and "历史内容" not in context.text
    )
    assert not recall(db, vector_settings, owner, None, "chat", [1, 0, 0]).text
    project = db.get(Project, project_id)
    project.worldbook, project.index_revision = "修订世界书", 2
    chapter = db.get(Chapter, chapter_id)
    chapter.content, chapter.version = paragraph_document(["修订正文"]), 2
    db.commit()
    assert not recall(db, vector_settings, owner, project_id, "writing", [1, 0, 0]).text
    await index_all(engine, vector_settings)
    assert (
        "修订正文"
        in recall(db, vector_settings, owner, project_id, "writing", [1, 0, 0], max_chars=4000).text
    )
    memory(
        db,
        owner,
        "已删除的派生日记事件",
        active=False,
        source_chapter_id=chapter_id,
        source_version=2,
        revision=2,
    )
    db.commit()
    assert (
        "修订正文"
        not in recall(
            db, vector_settings, owner, project_id, "writing", [1, 0, 0], max_chars=4000
        ).text
    )


@pytest.mark.asyncio
async def test_large_source_batches_and_metadata_only_references(
    db, engine, vector_settings, library
):
    vector_settings = vector_settings.model_copy(update={"embedding_batch_size": 10})
    owner, _, project_id, _, _ = library
    project = db.get(Project, project_id)
    project.worldbook = "世界" * 15000
    project.tentative = "最近：这仅是待定想法，尚未确认为事实。"
    db.commit()
    batches = []

    async def embed(*args):
        texts = args[-1]
        batches.append(texts)
        assert len(texts) <= 10
        assert sum(len(text.encode()) for text in texts) <= 32000
        return await vectors(*args)

    assert await index_all(engine, vector_settings, embed) >= 3
    assert sum(len(text) for batch in batches for text in batch) == 30000 + len(project.tentative)
    context = recall(
        db, vector_settings, owner, project_id, "writing", [1, 0, 0], max_chars=550, limit=1
    )
    assert len(context.text) <= 550 and '"field": "tentative"' in context.text
    task = Task(user_id=owner, kind="scene", project_id=project_id)
    db.add(task)
    db.flush()
    record_references(db, task.id, context)
    record_references(db, task.id, context)
    assert load_task_references(db, task.id) == context.references
    assert len(list(db.scalars(select(RetrievalReference)))) == 1
    assert (
        recall(db, vector_settings, owner, project_id, "writing", [1, 0, 0], max_chars=10).text
        == ""
    )


@pytest.mark.asyncio
async def test_reconfigure_space_hides_old_vectors_and_indexes_new_dimensions(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    memory(db, owner)
    db.commit()
    await index_all(engine, vector_settings)
    changed = vector_settings.model_copy(
        update={"embedding_dimensions": 2, "embedding_model": "new-model"}
    )
    assert embedding_space(changed) != embedding_space(vector_settings)
    assert not has_ready_context(db, changed, owner, project, "chat")
    assert not recall(db, changed, owner, project, "chat", [1, 0]).text

    async def embed(*args):
        return [[1, 0] for _ in args[-1]]

    await index_all(engine, changed, embed)
    assert recall(db, changed, owner, project, "chat", [1, 0]).text
    assert not has_ready_context(db, vector_settings, owner, project, "chat")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,status",
    [
        (ModelCallFailed("failed"), "failed"),
        (ModelCallUnknown("unknown"), "outcome_unknown"),
        (BudgetExceeded("Task model allowance exhausted"), "failed"),
    ],
)
async def test_terminal_attempt_is_not_recreated_until_source_changes(
    db, engine, vector_settings, library, failure, status
):
    owner, _, _, _, _ = library
    item = memory(db, owner)
    item_id = item.id
    db.commit()
    tasks = ensure_index_tasks(db, vector_settings)
    task_id = tasks[0].id
    assert [t.id for t in ensure_index_tasks(db, vector_settings)] == [task_id]
    db.commit()

    async def fail(*args):
        raise failure

    await execute_index(vector_settings, engine, task_id, embed=fail)
    db.expire_all()
    assert db.get(Task, task_id).status == status
    assert ensure_index_tasks(db, vector_settings) == []
    item = db.get(Memory, item_id)
    item.content, item.revision = "新内容", 2
    db.commit()
    assert ensure_index_tasks(db, vector_settings)[0].id != task_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [BudgetExceeded("Daily model allowance exhausted"), ModelBusy("occupied")]
)
async def test_budget_and_busy_delay_same_index_task(db, engine, vector_settings, library, failure):
    owner, _, _, _, _ = library
    memory(db, owner)
    db.commit()
    task = ensure_index_tasks(db, vector_settings)[0]
    task_id = task.id
    task.queue_job_id = 123
    db.commit()

    async def fail(*args):
        raise failure

    with pytest.raises(JobRetry):
        await execute_index(vector_settings, engine, task_id, embed=fail)
    db.expire_all()
    assert db.get(Task, task_id).status == "pending"
    assert ensure_index_tasks(db, vector_settings) == []
    db.commit()
    await execute_index(vector_settings, engine, task_id, embed=vectors)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.asyncio
async def test_source_edit_during_embedding_discards_result(db, engine, vector_settings, library):
    owner, _, project, _, _ = library
    item_id = memory(db, owner).id
    db.commit()
    task_id = ensure_index_tasks(db, vector_settings)[0].id
    db.commit()

    async def changed(*args):
        with Session(engine) as writer, writer.begin():
            item = writer.get(Memory, item_id)
            item.content, item.revision = "请求途中已纠正", 2
        return await vectors(*args)

    await execute_index(vector_settings, engine, task_id, embed=changed)
    db.expire_all()
    assert db.get(Task, task_id).status == "cancelled"
    assert not list(db.scalars(select(IndexChunk)))
    assert not has_ready_context(db, vector_settings, owner, project, "chat")
    assert ensure_index_tasks(db, vector_settings)[0].id != task_id


@pytest.mark.asyncio
async def test_life_metadata_and_reference_permissions_are_live(
    db, engine, vector_settings, library
):
    owner, _, project, _, _ = library
    item = memory(db, owner, "做过一个海边的梦", life_kind="dream", occurred_at=datetime.now(UTC))
    item.kind = "life"
    item_id = item.id
    db.commit()
    await index_all(engine, vector_settings)
    context = recall(db, vector_settings, owner, project, "chat", [1, 0, 0])
    assert '"life_kind": "dream"' in context.text and '"shared_at": null' in context.text
    assert str(item_id) in context.text
    item = db.get(Memory, item_id)
    item.shared_at = datetime.now(UTC)
    db.commit()
    assert (
        '"shared_at": null'
        not in recall(db, vector_settings, owner, project, "chat", [1, 0, 0]).text
    )
    item.active = False
    db.commit()
    assert not recall(db, vector_settings, owner, project, "chat", [1, 0, 0]).text


def test_disabled_embedding_configuration_never_schedules(db, settings, library):
    owner, _, _, _, _ = library
    memory(db, owner)
    db.commit()
    assert ensure_index_tasks(db, settings.model_copy(update={"embedding_model": ""})) == []
    assert not list(db.scalars(select(IndexState)))
