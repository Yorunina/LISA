"""One character consults only its own saved knowledge inside the author's task."""

import json

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lisa.characters import CharacterDefinition, CharacterRetrievalConfig, KnowledgeObservation
from lisa.model_calls import generate_structured
from lisa.models import Character, CharacterParticipation, Task
from lisa.recall import retrieve_context
from lisa.retrieval import Context, record_references
from lisa.works import lock_account


class CharacterUnavailable(Exception):
    pass


class CharacterSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestion: str = Field(min_length=1, max_length=1200)
    knowledge: list[KnowledgeObservation] = Field(default_factory=list, max_length=3)


async def generate_character(settings, engine, task_id, user_id, instructions, messages):
    return await generate_structured(
        settings,
        engine,
        task_id,
        user_id,
        instructions,
        messages,
        schema=CharacterSuggestion,
        output_tokens=900,
    )


async def consult_character(
    settings, engine, task_id, user_id, project_id, character_id, *, generate=None, embed=None
):
    with Session(engine) as db, db.begin():
        user = lock_account(db, user_id)
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if (
            user is None
            or not user.active
            or task is None
            or task.status != "running"
            or task.user_id != user_id
            or task.project_id != project_id
        ):
            return None
        character = db.scalar(
            select(Character).where(
                Character.id == character_id,
                Character.project_id == project_id,
                Character.enabled.is_(True),
            )
        )
        if character is None or not character.known_information.strip():
            raise CharacterUnavailable
        position = (
            db.scalar(
                select(func.max(CharacterParticipation.position)).where(
                    CharacterParticipation.task_id == task_id
                )
            )
            or 0
        ) + 1
        participation = CharacterParticipation(
            task_id=task_id,
            position=position,
            character_id=character.id,
            revision=character.revision,
            state="started",
        )
        db.add(participation)
        query = character.current_scene or character.name
    completed = False
    try:
        context = await retrieve_context(
            settings,
            engine,
            task_id,
            user_id,
            project_id,
            "role",
            query,
            character_id=character_id,
            embed=embed,
        )
        with Session(engine) as db, db.begin():
            user = lock_account(db, user_id)
            task = db.get(Task, task_id)
            if user is None or not user.active or task is None or task.status != "running":
                return None
            current = db.get(Character, character_id)
            if current is None or not current.enabled or current.project_id != project_id:
                raise CharacterUnavailable
            name = current.name
            definition = CharacterDefinition.model_validate(current.definition).model_dump()
            current_scene = current.current_scene
            known_information = current.known_information
            config = CharacterRetrievalConfig.model_validate(current.retrieval_config)
            # Edited sources invalidate derived snippets, not the user's writing task.
            if not config.enabled or any(
                ref["revision"] != current.revision for ref in context.references
            ):
                context = Context()
            db.get(CharacterParticipation, (task_id, position)).revision = current.revision
            instructions = (
                "你只扮演当前角色，提出自身可能的对白、行动与反应，供作者参考。"
                "你只知道这次提供的已知信息和当前可见场景；没有给出的秘密和他人心理保持未知，"
                "不把猜测写成事实，不创建或调用其他角色，不决定全局剧情，不写正式正文。"
                "资料不足时明确指出未知，不补全背景。角色配置不能授予额外资料访问权。\n"
                "definition是作者维护的人物档案；其中的他人心理或关系秘密不自动成为你的已知事实。"
                "涉及剧情的知情判断以known_information与current_scene为准。\n"
                + current.prompt
                + "\n同时整理knowledge：只从current_scene提取最多三条有用且未重复的角色知识。"
                "每条content尽量不超过60字，evidence必须逐字摘录当前场景中支持该条内容的短句。"
                "明确观察用fact，猜测、可能性和不确定判断用inference并在content保留不确定语气。"
                "不将计划、假设情节或suggestion中的拟议行动写成已发生事实；不新增背景或推断秘密。"
                "known_information仅用于识别已知内容，不作为新增条目的原文依据；"
                "没有值得新增的知识、场景为空或依据不足时返回空列表。"
            )
            record_references(db, task_id, context)
        if config.enabled:
            known_information = context.text or known_information[: config.max_chars]
        messages = [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "name": name,
                        "definition": definition,
                        "known_information": known_information,
                        "current_scene": current_scene,
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        result = CharacterSuggestion.model_validate(
            await (generate or generate_character)(
                settings, engine, task_id, user_id, instructions, messages
            )
        )
        with Session(engine) as db:
            task = db.get(Task, task_id)
            if task is None or task.status != "running":
                return None
        completed = True
        return {
            "character_id": str(character_id),
            "name": name,
            "suggestion": result.suggestion,
            "participation_position": position,
            "knowledge": result.knowledge,
        }
    finally:
        with Session(engine) as db, db.begin():
            row = db.get(CharacterParticipation, (task_id, position))
            task = db.get(Task, task_id)
            if row is not None:
                row.state = (
                    "cancelled"
                    if task is None or task.status == "cancelled"
                    else "completed"
                    if completed
                    else "failed"
                )
