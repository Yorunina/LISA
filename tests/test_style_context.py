"""Applied configurations reach real contexts without changing an in-flight task."""

from uuid import UUID

import pytest
from procrastinate.exceptions import JobRetry
from sqlalchemy import select
from test_author import pending_task
from test_chat import ingest
from test_chat import user_id as user_id
from test_works import HEADERS, RESULT
from test_works import owner as owner
from test_works import project as project

from lisa.author import execute_author
from lisa.chat import execute_chat
from lisa.model_calls import ModelBusy
from lisa.models import Persona, Task


@pytest.mark.asyncio
async def test_author_pins_applied_style_and_next_task_reads_new_default(
    settings, engine, client, db, owner, project
):
    def profile(name, prompt):
        response = client.post(
            "/api/style/profiles",
            json={"kind": "writing", "name": name, "prompt": prompt},
            headers=HEADERS,
        )
        assert response.status_code == 201, response.text
        return response.json()["versions"][0]["id"]

    original = profile("原笔法", "合成甲风格：描写从远处的声音开始。")
    newer = profile("新笔法", "合成乙风格：以人物动作推动短句。")
    assert client.put(
        "/api/style/applications/writing", json={"version_id": original}, headers=HEADERS
    ).status_code == 200
    first = pending_task(db, owner, project)
    seen = []

    async def first_generation(*args):
        seen.append(args[-2])
        assert client.put(
            "/api/style/applications/writing", json={"version_id": newer}, headers=HEADERS
        ).status_code == 200
        return RESULT

    await execute_author(settings, engine, first, generate=first_generation)
    db.expire_all()
    first_task = db.get(Task, first)
    assert first_task.status == "completed"
    assert first_task.input_snapshot["writing_style"]["effective_version_id"] == original
    assert "合成甲风格" in seen[0] and "合成乙风格" not in seen[0]

    second = pending_task(db, owner, project)

    async def next_generation(*args):
        seen.append(args[-2])
        return RESULT

    await execute_author(settings, engine, second, generate=next_generation)
    db.expire_all()
    assert db.get(Task, second).input_snapshot["writing_style"]["effective_version_id"] == newer
    assert "合成乙风格" in seen[1] and "合成甲风格" not in seen[1]


@pytest.mark.asyncio
async def test_chat_model_slot_retry_keeps_started_persona_snapshot(settings, engine, db, user_id):
    persona = db.scalar(select(Persona).where(Persona.user_id == user_id))
    original_prompt = persona.prompt
    task_id = ingest(db, "style-persona-before")

    async def busy(*args):
        raise ModelBusy("synthetic occupied model slot")

    with pytest.raises(JobRetry):
        await execute_chat(settings, engine, task_id, generate=busy)
    db.expire_all()
    persona = db.scalar(select(Persona).where(Persona.user_id == user_id))
    persona.prompt = "后来保存的新性格，仅对下一次交流生效。"
    persona.version += 1
    db.commit()
    instructions = []

    async def generate(*args):
        instructions.append(args[-2])
        return "合成回复。"

    await execute_chat(settings, engine, task_id, generate=generate)
    assert original_prompt in instructions[0]
    assert "后来保存的新性格" not in instructions[0]
    next_id = ingest(db, "style-persona-after")
    await execute_chat(settings, engine, next_id, generate=generate)
    assert "后来保存的新性格" in instructions[1]
    db.expire_all()
    assert db.get(Task, UUID(str(task_id))).status == "completed"
